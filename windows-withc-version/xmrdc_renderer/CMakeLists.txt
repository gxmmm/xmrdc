cmake_minimum_required(VERSION 3.10)
project(xmrdc_renderer)

set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# 查找FFmpeg
find_path(AVCODEC_INCLUDE_DIR libavcodec/avcodec.h)
find_library(AVCODEC_LIBRARY avcodec)

find_path(AVFORMAT_INCLUDE_DIR libavformat/avformat.h)
find_library(AVFORMAT_LIBRARY avformat)

find_path(AVUTIL_INCLUDE_DIR libavutil/imgutils.h)
find_library(AVUTIL_LIBRARY avutil)

find_path(SWSCALE_INCLUDE_DIR libswscale/swscale.h)
find_library(SWSCALE_LIBRARY swscale)

include_directories(
    ${AVCODEC_INCLUDE_DIR}
    ${AVFORMAT_INCLUDE_DIR}
    ${AVUTIL_INCLUDE_DIR}
    ${SWSCALE_INCLUDE_DIR}
)

add_executable(xmrdc_renderer xmrdc_renderer.cpp)

target_link_libraries(xmrdc_renderer
    ${AVCODEC_LIBRARY}
    ${AVFORMAT_LIBRARY}
    ${AVUTIL_LIBRARY}
    ${SWSCALE_LIBRARY}
    gdi32
)

if(MSVC)
    target_compile_definitions(xmrdc_renderer PRIVATE _CRT_SECURE_NO_WARNINGS)
endif()