#pragma once
#include <algorithm>
#include <cstdint>

namespace panorama {
constexpr int WIDTH = 272, HEIGHT = 320, OUT_WIDTH = 384, LEFT_X = 56;
constexpr int INPUT_SIZE = WIDTH * HEIGHT * 3 / 2;
constexpr int OUTPUT_SIZE = OUT_WIDTH * HEIGHT * 3 / 2;

inline void blend(const uint8_t *left, const uint8_t *right, bool right_valid,
                  int dx, int dy, uint8_t *output) {
  std::fill_n(output, OUT_WIDTH * HEIGHT, 16);
  std::fill_n(output + OUT_WIDTH * HEIGHT, OUT_WIDTH * HEIGHT / 2, 128);
  const int right_x = LEFT_X + dx;
  // Keep the left camera's centre intact: it is also the hand model's
  // coordinate space. Blend the right view only at the edge where it extends
  // coverage, since stereo disparity varies with scene depth.
  const int seam = dx < 0 ? LEFT_X + 8 : LEFT_X + WIDTH - 8;
  auto plane = [&](int rows, int div, int in_offset, int out_offset) {
    for (int y = 0; y < rows; ++y) for (int x = 0; x < OUT_WIDTH; ++x) {
      const int lx = x - LEFT_X, rx = x - right_x, ry = y - dy / div;
      const bool l = lx >= 0 && lx < WIDTH;
      const bool r = right_valid && dx != 0 && rx >= 0 && rx < WIDTH && ry >= 0 && ry < rows;
      if (!l && !r) continue;
      const int lv = l ? left[in_offset + y * WIDTH + lx] : 0;
      const int rv = r ? right[in_offset + ry * WIDTH + rx] : 0;
      int value = l ? lv : rv;
      if (l && r) {
        float weight = std::clamp((x - seam + 8) / 16.0f, 0.0f, 1.0f);
        if (dx < 0) weight = 1.0f - weight;
        value = static_cast<int>((1 - weight) * lv + weight * rv);
      }
      output[out_offset + y * OUT_WIDTH + x] = static_cast<uint8_t>(value);
    }
  };
  plane(HEIGHT, 1, 0, 0);
  plane(HEIGHT / 2, 2, WIDTH * HEIGHT, OUT_WIDTH * HEIGHT);
}
}  // namespace panorama
