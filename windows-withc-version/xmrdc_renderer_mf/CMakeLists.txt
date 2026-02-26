cmake_minimum_required(VERSION 3.10)
project(xmrdc_renderer_mf)

set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# 仅Windows平台支持
if(NOT WIN32)
    message(FATAL_ERROR "This project only supports Windows platform")
endif()

# 添加Media Foundation依赖
set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} mfplat.lib mfreadwrite.lib mfuuid.lib mf.lib d3d9.lib dxva2.lib")

# 构建使用Media Foundation的版本
add_executable(xmrdc_renderer_mf xmrdc_renderer_mf.cpp)

# 设置编译选项
if(MSVC)
    target_compile_definitions(xmrdc_renderer_mf PRIVATE _CRT_SECURE_NO_WARNINGS)
    target_compile_options(xmrdc_renderer_mf PRIVATE /EHsc)
endif()

# 复制可执行文件到上级目录
add_custom_command(TARGET xmrdc_renderer_mf POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy $<TARGET_FILE:xmrdc_renderer_mf> "${CMAKE_SOURCE_DIR}/../"
)