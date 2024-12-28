cmake_minimum_required(VERSION 3.5)

project(sky_scripter)

find_package(Ceres REQUIRED)
# Add glog and gflags
find_package(glog REQUIRED)
find_package(gflags REQUIRED)

# Set output directory to bin
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_SOURCE_DIR}/bin)

add_executable(polar_align src/polar_align_main.cc)
target_link_libraries(polar_align Ceres::ceres)