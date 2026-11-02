// One capture graph and one process own all BPU models. Page demand only gates frames.
#include <cstdlib>
#include <iostream>
#include <memory>
#include <sstream>
#include <string>
#include <vector>
#include <ament_index_cpp/get_package_prefix.hpp>
#include <ament_index_cpp/get_resource.hpp>
#include <class_loader/class_loader.hpp>
#include <rclcpp/rclcpp.hpp>
#include <rclcpp_components/node_factory.hpp>
#include "hand_lmk_det_node.h"
#include "hand_gesture_det_node.h"

std::shared_ptr<rclcpp::Node> make_camera_throttle(const rclcpp::NodeOptions &);
std::shared_ptr<rclcpp::Node> make_depth_input(const rclcpp::NodeOptions &);
std::shared_ptr<rclcpp::Node> make_stereo_panorama(const rclcpp::NodeOptions &);
using Parameter = rclcpp::Parameter;

static rclcpp::NodeOptions options(const std::string &name,
                                  std::vector<Parameter> parameters,
                                  const std::string &level = "error") {
  rclcpp::NodeOptions result;
  result.parameter_overrides(parameters);
  result.arguments({"--ros-args", "-r", "__node:=" + name,
                    "--log-level", name + ":=" + level});
  // The vendor camera/body components require HBM loaned messages. Do not enable
  // intra-process mode globally: it would disable those loans in Humble.
  if (name == "muse_body" || name == "muse_hands" || name == "muse_yolo") {
    const std::string topic = name == "muse_yolo" ?
        "/muse/camera/panorama/model/hbmem" : "/muse/camera/left/hbmem";
    auto arguments = result.arguments();
    arguments.insert(arguments.end(), {"-r", "/hbmem_img:=" + topic, "-r", "/image_raw:=" + topic});
    result.arguments(arguments);
  }
  result.use_intra_process_comms(false);
  return result;
}

class Runtime {
 public:
  explicit Runtime(rclcpp::Executor &executor) : executor_(executor) {}
  ~Runtime() { rclcpp::shutdown(); }
  void add(const std::shared_ptr<rclcpp::Node> &node) {
    executor_.add_node(node); nodes_.push_back(node);
  }
  void component(const std::string &package, const std::string &class_name,
                 const rclcpp::NodeOptions &node_options) {
    std::string resource, prefix;
    if (!ament_index_cpp::get_resource("rclcpp_components", package, resource, &prefix))
      throw std::runtime_error("Component registry missing: " + package);
    std::istringstream lines(resource); std::string line, library;
    while (std::getline(lines, line)) {
      const auto separator = line.find(';');
      if (separator != std::string::npos && line.substr(0, separator) == class_name)
        library = prefix + "/" + line.substr(separator + 1);
    }
    if (library.empty()) throw std::runtime_error("Component not registered: " + class_name);
    auto loader = std::make_unique<class_loader::ClassLoader>(library);
    auto classes = loader->getAvailableClasses<rclcpp_components::NodeFactory>();
    for (const auto &factory_name : classes) {
      if (factory_name.find(class_name) == std::string::npos) continue;
      auto factory = loader->createInstance<rclcpp_components::NodeFactory>(factory_name);
      auto instance = factory->create_node_instance(node_options);
      executor_.add_node(instance.get_node_base_interface());
      components_.push_back(instance); loaders_.push_back(std::move(loader));
      return;
    }
    throw std::runtime_error("Component factory missing: " + class_name);
  }
 private:
  rclcpp::Executor &executor_;
  std::vector<std::unique_ptr<class_loader::ClassLoader>> loaders_;
  std::vector<rclcpp_components::NodeInstanceWrapper> components_;
  std::vector<std::shared_ptr<rclcpp::Node>> nodes_;
};

static std::string libpath(const std::string &package, const std::string &name) {
  return ament_index_cpp::get_package_prefix(package) + "/lib/" + package + "/config/" + name;
}

int main(int argc, char **argv) {
  rclcpp::init(argc, argv);
  try {
    rclcpp::executors::MultiThreadedExecutor executor(rclcpp::ExecutorOptions(), 4);
    Runtime runtime(executor);
    runtime.component("mipi_cam", "mipi_cam::MipiCamNode", options("muse_camera", {
      {"config_path", ament_index_cpp::get_package_prefix("mipi_cam") + "/lib/mipi_cam/config/"},
      {"camera_calibration_file_path", "default"}, {"device_mode", "dual"},
      {"dual_combine", 1}, {"image_width", 640}, {"image_height", 352},
      {"framerate", 30.0}, {"out_format", "nv12"}, {"io_method", "shared_mem"},
      {"frame_ts_type", "realtime"}, {"gdc_enable", true}, {"lpwm_enable", true},
      {"rotation", 0.0}, {"cal_rotation", 0.0}, {"stream_mode", 1},
      {"channel", 0}, {"channel2", 2}, {"sub_stream_enable", true},
      {"sub_image_width", 272}, {"sub_image_height", 320}}, "warn"));
    runtime.add(make_camera_throttle(options("muse_camera_throttle", {
      {"fps", 10.0}, {"preview_fps", 30.0}, {"image_transport", "shared_mem"},
      {"left_topic", "/sub_hbmem_left_img"}, {"right_topic", "/sub_hbmem_right_img"}}, "info")));
    runtime.add(make_depth_input(options("muse_depth_input", {{"fps", 30.0}})));
    runtime.add(make_stereo_panorama(options("muse_stereo_panorama", {})));
    runtime.component("mono2d_body_detection", "Mono2dBodyDetNode", options("muse_body", {
      {"model_file_name", libpath("mono2d_body_detection", "multitask_body_head_face_hand_kps_960x544.hbm")},
      {"model_type", 0}, {"track_mode", 1}, {"image_gap", 0}, {"is_shared_mem_sub", 1},
      {"ros_img_topic_name", "/muse/camera/left/hbmem"}, {"ai_msg_pub_topic_name", "/muse/body"}}));
    runtime.add(std::make_shared<HandLmkDetNode>("muse_hands", options("muse_hands", {
      {"feed_type", 0}, {"model_file_name", libpath("hand_lmk_detection", "handLMKs.hbm")},
      {"is_shared_mem_sub", 1}, {"ros_img_topic_name", "/muse/camera/left/hbmem"},
      {"ai_msg_sub_topic_name", "/muse/body"}, {"ai_msg_pub_topic_name", "/muse/hand_landmarks"}})));
    runtime.add(std::make_shared<inference::HandGestureDetNode>("muse_gestures", options("muse_gestures", {
      {"model_file_name", libpath("hand_gesture_detection", "gestureDet_8x21.hbm")},
      {"model_name", "gestureDet_8x21"}, {"is_dynamic_gesture", false},
      {"threshold", 0.5}, {"time_interval_sec", 0.25},
      {"ai_msg_sub_topic_name", "/muse/gesture_input"}, {"ai_msg_pub_topic_name", "/muse/gestures"}})));
    const char *yolo = std::getenv("MUSE_RUNTIME_YOLO_CONFIG");
    if (!yolo) throw std::runtime_error("MUSE_RUNTIME_YOLO_CONFIG is not prepared");
    runtime.component("dnn_node_example", "DnnExampleNode", options("muse_yolo", {
      {"feed_type", 1}, {"is_shared_mem_sub", 1},
      {"sharedmem_img_topic_name", "/muse/camera/panorama/model/hbmem"},
      {"ros_img_topic_name", "/muse/camera/panorama/model/hbmem"}, {"config_file", std::string(yolo)},
      {"msg_pub_topic_name", "/muse/objects"}, {"dump_render_img", 0}}));
    const auto stereo = ament_index_cpp::get_package_prefix("hobot_stereonet") +
        "/share/hobot_stereonet/config/DStereoV2.4_int8.bin";
    runtime.component("hobot_stereonet", "stereonet::StereoNetNode", options("MuseStereo", {
      {"stereonet_model_file_path", stereo}, {"stereo_image_topic", "/muse/depth/input"},
      {"camera_info_topic", "/hbmem_combine_img/right/camera_info"},
      {"left_camera_info_topic", "/hbmem_combine_img/left/camera_info"},
      {"publish_pcd_enabled", false}, {"publish_origin_enable", false},
      {"publish_rectify_bgr", false}, {"publish_visual_enabled", true},
      {"render_type", "distance"}, {"render_perf", false},
      {"infer_thread_num", 2}, {"save_result_flag", false}}, "warn"));
    if (!rclcpp::ok()) throw std::runtime_error("Vendor model initialization failed");
    std::cout << "Muse single-process vision runtime ready: camera + body + hands + gesture + YOLO + depth" << std::endl;
    executor.spin();
  } catch (const std::exception &error) {
    std::cerr << "Muse vision runtime failed: " << error.what() << std::endl;
    rclcpp::shutdown(); return 1;
  }
  rclcpp::shutdown(); return 0;
}
