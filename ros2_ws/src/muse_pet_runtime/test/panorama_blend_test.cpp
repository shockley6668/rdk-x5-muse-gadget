#include "../src/panorama_blend.hpp"
#include <cassert>
#include <cstdint>
#include <vector>

int main() {
  using namespace panorama;
  std::vector<uint8_t> left(INPUT_SIZE, 30), right(INPUT_SIZE, 200), output(OUTPUT_SIZE);
  std::fill(left.begin() + WIDTH * HEIGHT, left.end(), 80);
  std::fill(right.begin() + WIDTH * HEIGHT, right.end(), 170);
  blend(left.data(), right.data(), true, 20, 0, output.data());
  assert(output[0] == 16);
  assert(output[LEFT_X] == 30);
  assert(output[LEFT_X + WIDTH / 2] == 30);
  assert(output[LEFT_X + WIDTH + 10] == 200);
  assert(output[OUT_WIDTH * HEIGHT + LEFT_X] == 80);
  assert(output[OUT_WIDTH * HEIGHT + LEFT_X + WIDTH + 10] == 170);
  blend(left.data(), right.data(), true, -20, 0, output.data());
  assert(output[LEFT_X + WIDTH / 2] == 30);
  assert(output[LEFT_X - 10] == 200);
  blend(left.data(), right.data(), true, 0, 0, output.data());
  assert(output[LEFT_X + WIDTH / 2] == 30);
  assert(output[LEFT_X + WIDTH - 1] == 30);
  blend(left.data(), nullptr, false, 20, 0, output.data());
  assert(output[LEFT_X] == 30);
  assert(output[LEFT_X + WIDTH + 10] == 16);
  return 0;
}
