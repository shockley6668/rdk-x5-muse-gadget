// Align the two NV12 substreams into a fixed-width panorama for one YOLO.
// StereoNet continues to consume the original calibrated stereo frames.
#include <algorithm>
#include <cmath>
#include <cstring>
#include <memory>
#include <mutex>
#include <vector>
#include <opencv2/core.hpp>
#include <opencv2/features2d.hpp>
#include <rclcpp/rclcpp.hpp>
#include <std_msgs/msg/bool.hpp>
#include <hbm_img_msgs/msg/hbm_msg1080_p.hpp>
#include "stream_budget.hpp"
#include "view_demand.hpp"
#include "panorama_blend.hpp"

using Frame = hbm_img_msgs::msg::HbmMsg1080P;
using Clock = muse::Clock;
namespace {
using namespace panorama;

struct Input {
  std::vector<uint8_t> data;
  builtin_interfaces::msg::Time stamp;
  uint64_t index = 0;
  bool valid = false;
};

int64_t stamp_ns(const builtin_interfaces::msg::Time &t) {
  return static_cast<int64_t>(t.sec) * 1000000000LL + t.nanosec;
}

class StereoPanorama : public rclcpp::Node {
 public:
  explicit StereoPanorama(const rclcpp::NodeOptions &options)
      : Node("muse_stereo_panorama", options), gate_(10.0), orb_(cv::ORB::create(400)) {
    model_ = create_publisher<Frame>("/muse/camera/panorama/model/hbmem", rclcpp::QoS(1).reliable());
    preview_ = create_publisher<Frame>("/muse/camera/panorama/preview/hbmem", rclcpp::QoS(1).reliable());
    if (!model_->can_loan_messages() || !preview_->can_loan_messages())
      throw std::runtime_error("Panorama requires shared memory image loans");
    demand_ = create_subscription<std_msgs::msg::Bool>("/muse/preview/demand",
        rclcpp::QoS(1).best_effort(), [this](const std_msgs::msg::Bool &msg) {
          std::lock_guard<std::mutex> lock(mu_); preview_lease_.update(msg.data, Clock::now());
        });
    for (size_t eye = 0; eye < 2; ++eye) {
      const auto topic = eye ? "/muse/camera/right/hbmem" : "/muse/camera/left/hbmem";
      inputs_[eye] = create_subscription<Frame>(topic, rclcpp::SensorDataQoS().keep_last(1),
          [this, eye](const Frame &msg) { receive(eye, msg); });
    }
  }

 private:
  void receive(size_t eye, const Frame &msg) {
    if (msg.width != WIDTH || msg.height != HEIGHT || msg.step != WIDTH ||
        msg.data_size != INPUT_SIZE || msg.data_size > msg.data.size() ||
        !std::equal(msg.encoding.begin(), msg.encoding.begin() + 4, "nv12")) return;
    std::lock_guard<std::mutex> lock(mu_);
    Input &in = eyes_[eye];
    in.data.assign(msg.data.begin(), msg.data.begin() + INPUT_SIZE);
    in.stamp = msg.time_stamp; in.index = msg.index; in.valid = true;
    const bool depth = muse::depth_requested();
    const bool capture = muse::depth_requested("/run/user/1000/muse-capture.active", std::time(nullptr));
    if (!eyes_[0].valid || (depth && !capture)) return;
    const auto delta = eyes_[1].valid ?
        std::llabs(stamp_ns(eyes_[0].stamp) - stamp_ns(eyes_[1].stamp)) : 1000000000LL;
    // Brief skew waits for a pair; a missing right eye falls back to left only.
    if (delta > 120000000LL && delta < 500000000LL) return;
    right_valid_ = delta <= 120000000LL;
    const auto now = Clock::now();
    if (!gate_.ready(now)) return;
    if (right_valid_ && now >= next_alignment_) {
      align(); next_alignment_ = now + std::chrono::seconds(2);
    }
    if (!depth && model_->get_subscription_count()) publish(model_);
    if (preview_lease_.active(now) && preview_->get_subscription_count()) publish(preview_);
  }

  void align() {
    const cv::Mat left(HEIGHT, WIDTH, CV_8UC1, eyes_[0].data.data());
    const cv::Mat right(HEIGHT, WIDTH, CV_8UC1, eyes_[1].data.data());
    std::vector<cv::KeyPoint> a, b;
    cv::Mat da, db;
    orb_->detectAndCompute(left, cv::noArray(), a, da);
    orb_->detectAndCompute(right, cv::noArray(), b, db);
    if (da.empty() || db.empty() || da.rows < 2) return;
    std::vector<std::vector<cv::DMatch>> pairs;
    cv::BFMatcher(cv::NORM_HAMMING).knnMatch(db, da, pairs, 2);
    std::vector<float> dx, dy;
    for (const auto &p : pairs) {
      if (p.size() < 2 || p[0].distance >= .72f * p[1].distance) continue;
      auto delta = a[p[0].trainIdx].pt - b[p[0].queryIdx].pt;
      if (std::abs(delta.x) <= 54 && std::abs(delta.y) <= 16) {
        dx.push_back(delta.x); dy.push_back(delta.y);
      }
    }
    if (dx.size() < 10) return;  // Keep last alignment when a scene lacks texture.
    const auto middle = dx.size() / 2;
    std::nth_element(dx.begin(), dx.begin() + middle, dx.end());
    std::nth_element(dy.begin(), dy.begin() + middle, dy.end());
    const float mx = dx[middle], my = dy[middle];
    size_t inliers = 0;
    // Independent median spread rejects uncertain matches.
    for (float x : dx) if (std::abs(x - mx) < 5) ++inliers;
    if (inliers < 8 || inliers * 2 < dx.size()) return;
    dx_ = std::clamp(2 * static_cast<int>(std::round(mx / 2)), -54, 54);
    dy_ = std::clamp(2 * static_cast<int>(std::round(my / 2)), -16, 16);
  }

  void publish(const rclcpp::Publisher<Frame>::SharedPtr &publisher) {
    auto loan = publisher->borrow_loaned_message();
    auto &out = loan.get();
    out.width = OUT_WIDTH; out.height = HEIGHT; out.step = OUT_WIDTH;
    out.data_size = OUTPUT_SIZE; out.time_stamp = eyes_[0].stamp; out.index = eyes_[0].index;
    out.encoding = {}; std::copy_n("nv12", 4, out.encoding.begin());
    // The fixed left origin lets the preview transform hand coordinates by 56.
    panorama::blend(eyes_[0].data.data(), eyes_[1].data.data(), right_valid_,
                    dx_, dy_, out.data.data());
    publisher->publish(std::move(loan));
  }

  std::mutex mu_;
  Input eyes_[2];
  bool right_valid_ = false;
  int dx_ = 0, dy_ = 0;
  Clock::time_point next_alignment_{};
  muse::RateGate gate_;
  muse::PreviewLease preview_lease_;
  cv::Ptr<cv::ORB> orb_;
  rclcpp::Subscription<Frame>::SharedPtr inputs_[2];
  rclcpp::Subscription<std_msgs::msg::Bool>::SharedPtr demand_;
  rclcpp::Publisher<Frame>::SharedPtr model_, preview_;
};
}  // namespace

std::shared_ptr<rclcpp::Node> make_stereo_panorama(const rclcpp::NodeOptions &options) {
  return std::make_shared<StereoPanorama>(options);
}
