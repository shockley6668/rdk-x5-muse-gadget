cmake_minimum_required(VERSION 3.16)
project(muse_pet_runtime)
find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rclcpp_components REQUIRED)
find_package(class_loader REQUIRED)
find_package(ament_index_cpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(std_msgs REQUIRED)
find_package(ai_msgs REQUIRED)
find_package(hbm_img_msgs REQUIRED)
find_package(dnn_node REQUIRED)
find_package(OpenCV REQUIRED)
set(HAND vendor/hand_lmk_detection)
set(GESTURE vendor/hand_gesture_detection)
add_executable(vision_runtime
  src/vision_runtime.cpp
  src/stereo_panorama.cpp
  ../muse_pet_camera_throttle/src/camera_throttle.cpp
  ../muse_pet_camera_throttle/src/depth_input.cpp
  ${HAND}/src/ai_msg_manage.cpp ${HAND}/src/hand_lmk_det_node.cpp ${HAND}/src/hand_lmk_output_parser.cpp
  ${GESTURE}/src/hand_gesture_det_node.cpp ${GESTURE}/src/gesture_preprocess.cpp
  ${GESTURE}/src/gesture_postprocess.cpp ${GESTURE}/src/gesture_postprocess_util.cc
  ${GESTURE}/src/lmks_process.cpp ${GESTURE}/src/threads/threadpool.cpp)
# Both legacy executables define the same global timing helper. Rename one at
# compile time so their unmodified algorithm sources can coexist in one binary.
set_source_files_properties(${HAND}/src/hand_lmk_det_node.cpp PROPERTIES
  COMPILE_DEFINITIONS "CalTimeMsDuration=MuseHandLmkCalTimeMsDuration")
target_compile_features(vision_runtime PRIVATE cxx_std_17)
target_compile_definitions(vision_runtime PRIVATE MUSE_RUNTIME_EMBEDDED SHARED_MEM_ENABLED)
target_include_directories(vision_runtime PRIVATE ../muse_pet_camera_throttle/include
  ${HAND} ${HAND}/include ${GESTURE} ${GESTURE}/include ${OpenCV_INCLUDE_DIRS})
ament_target_dependencies(vision_runtime rclcpp rclcpp_components class_loader ament_index_cpp
  sensor_msgs std_msgs ai_msgs hbm_img_msgs dnn_node)
target_link_directories(vision_runtime PRIVATE /usr/lib/hbbpu /usr/hobot/lib /usr/lib/hbmedia)
target_link_libraries(vision_runtime ${OpenCV_LIBS})
install(TARGETS vision_runtime DESTINATION lib/${PROJECT_NAME})
ament_package()
