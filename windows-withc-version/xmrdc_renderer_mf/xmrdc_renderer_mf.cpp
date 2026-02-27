#include <windows.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mfreadwrite.h>
#include <mftransform.h>
#include <mferror.h>
#include <iostream>
#include <string>
#include <thread>
#include <mutex>
#include <atomic>
#include <vector>
#include <cstring>

#pragma comment(lib, "mfplat.lib")
#pragma comment(lib, "mfreadwrite.lib")
#pragma comment(lib, "mfuuid.lib")
#pragma comment(lib, "mf.lib")
#pragma comment(lib, "d3d9.lib")
#pragma comment(lib, "dxva2.lib")

const int SHM_SIZE = 10 * 1024 * 1024;
const int MAX_FRAME_SIZE = 8 * 1024 * 1024;

// 字节序转换函数 - 正确实现
uint32_t ntohl_fixed(uint32_t netlong) {
    return ((netlong >> 24) & 0x000000FF) |
           ((netlong >> 8) & 0x0000FF00) |
           ((netlong << 8) & 0x00FF0000) |
           ((netlong << 24) & 0xFF000000);
}

// 直接从内存读取网络字节序数据
uint32_t read_uint32_network(const unsigned char* buffer) {
    return ((uint32_t)buffer[0] << 24) |
           ((uint32_t)buffer[1] << 16) |
           ((uint32_t)buffer[2] << 8) |
           ((uint32_t)buffer[3]);
}

class H264Renderer {
private:
    HWND target_window;
    HANDLE shm_handle;
    unsigned char* shm_buffer;
    std::string shm_name;
    
    int width;
    int height;
    std::atomic<bool> running;
    std::mutex frame_mutex;
    
    HDC hdc_window;
    HDC hdc_mem;
    HBITMAP hbitmap;
    BITMAPINFO bmi;
    
    unsigned char* rgb_buffer;
    int rgb_buffer_size;

    // Media Foundation相关
    IMFTransform* pDecoder;
    IMFMediaType* pInputMediaType;
    IMFMediaType* pOutputMediaType;
    IMFSample* pSample;
    IMFMediaBuffer* pMediaBuffer;

public:
    H264Renderer(HWND hwnd, const std::string& shm_name, int w, int h)
        : target_window(hwnd), shm_name(shm_name), width(w), height(h),
          running(false), shm_handle(nullptr), shm_buffer(nullptr),
          hdc_window(nullptr), hdc_mem(nullptr),
          hbitmap(nullptr), rgb_buffer(nullptr),
          pDecoder(nullptr), pInputMediaType(nullptr),
          pOutputMediaType(nullptr), pSample(nullptr),
          pMediaBuffer(nullptr) {
        
        std::cout << "[C++ Renderer] Initialize renderer (Media Foundation)" << std::endl;
        std::cout << "[C++ Renderer] Window handle: " << target_window << std::endl;
        std::cout << "[C++ Renderer] Shared memory: " << shm_name << std::endl;
        std::cout << "[C++ Renderer] Resolution: " << width << "x" << height << std::endl;
    }

    ~H264Renderer() {
        stop();
        cleanup();
    }

    bool init() {
        // 初始化COM
        if (FAILED(CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE))) {
            std::cerr << "[C++ Renderer] COM initialization failed" << std::endl;
            return false;
        }

        // 初始化Media Foundation
        if (FAILED(MFStartup(MF_VERSION))) {
            std::cerr << "[C++ Renderer] Media Foundation initialization failed" << std::endl;
            return false;
        }
        
        // 处理DPI缩放问题
        SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
        printf("[C++ Renderer] DPI awareness set to per-monitor aware v2\n");

        if (!init_shared_memory()) {
            std::cerr << "[C++ Renderer] Shared memory initialization failed" << std::endl;
            return false;
        }

        if (!init_decoder()) {
            std::cerr << "[C++ Renderer] Decoder initialization failed" << std::endl;
            return false;
        }

        if (!init_gdi()) {
            std::cerr << "[C++ Renderer] GDI initialization failed" << std::endl;
            return false;
        }

        running = true;
        return true;
    }

    void start() {
        std::cout << "[C++ Renderer] Start render thread" << std::endl;
        std::thread render_thread(&H264Renderer::render_loop, this);
        render_thread.detach();
    }

    void stop() {
        running = false;
    }

    void update_resolution(int new_width, int new_height) {
        std::lock_guard<std::mutex> lock(frame_mutex);
        
        printf("[C++ Renderer] Update resolution: %dx%d\n", new_width, new_height);
        
        width = new_width;
        height = new_height;
        
        // 先更新GDI，确保RGB缓冲区大小正确
        update_gdi();
        
        // 重置解码器状态
        if (pDecoder) {
            pDecoder->ProcessMessage(MFT_MESSAGE_COMMAND_FLUSH, 0);
            pDecoder->ProcessMessage(MFT_MESSAGE_NOTIFY_END_STREAMING, 0);
            pDecoder->ProcessMessage(MFT_MESSAGE_NOTIFY_END_OF_STREAM, 0);
        }
        
        // 不需要完全重新初始化解码器，只需要更新输出格式
        update_decoder_format();
    }

private:
    bool init_shared_memory() {
        shm_handle = CreateFileMappingA(
            INVALID_HANDLE_VALUE,
            nullptr,
            PAGE_READWRITE,
            0,
            SHM_SIZE,
            shm_name.c_str()
        );

        if (shm_handle == nullptr) {
            std::cerr << "[C++ Renderer] Create shared memory failed: " << GetLastError() << std::endl;
            return false;
        }

        shm_buffer = (unsigned char*)MapViewOfFile(
            shm_handle,
            FILE_MAP_ALL_ACCESS,
            0,
            0,
            SHM_SIZE
        );

        if (shm_buffer == nullptr) {
            std::cerr << "[C++ Renderer] Map shared memory failed: " << GetLastError() << std::endl;
            CloseHandle(shm_handle);
            return false;
        }

        std::cout << "[C++ Renderer] Shared memory initialized successfully" << std::endl;
        return true;
    }

    bool init_decoder() {
        // 创建H264解码器
        if (FAILED(CoCreateInstance(
            CLSID_MSH264DecoderMFT,
            nullptr,
            CLSCTX_INPROC_SERVER,
            IID_PPV_ARGS(&pDecoder)))) {
            std::cerr << "[C++ Renderer] Create H264 decoder failed" << std::endl;
            return false;
        }

        // 设置输入媒体类型为H264
        if (FAILED(MFCreateMediaType(&pInputMediaType))) {
            std::cerr << "[C++ Renderer] Create input media type failed" << std::endl;
            return false;
        }

        if (FAILED(pInputMediaType->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video))) {
            std::cerr << "[C++ Renderer] Set input media type failed" << std::endl;
            return false;
        }

        if (FAILED(pInputMediaType->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_H264))) {
            std::cerr << "[C++ Renderer] Set input subtype failed" << std::endl;
            return false;
        }

        if (FAILED(pDecoder->SetInputType(0, pInputMediaType, 0))) {
            std::cerr << "[C++ Renderer] Set decoder input type failed" << std::endl;
            return false;
        }

        // 枚举可能的输出类型
        IMFMediaType* pOutputMediaTypeEnum = nullptr;
        HRESULT hr = S_OK;
        DWORD index = 0;

        printf("[C++ Renderer] Enumerating available output formats:\n");
        while (SUCCEEDED(hr)) {
            hr = pDecoder->GetOutputAvailableType(0, index, &pOutputMediaTypeEnum);
            if (SUCCEEDED(hr)) {
                GUID subtype = GUID_NULL;
                pOutputMediaTypeEnum->GetGUID(MF_MT_SUBTYPE, &subtype);
                
                // 打印所有可用格式
                printf("[C++ Renderer]   Format %d: %08X-%04X-%04X\n", index, subtype.Data1, subtype.Data2, subtype.Data3);
                
                // 尝试使用YUV或RGB格式（优先使用YUY2，因为它是Windows H264解码器的常用输出格式）
                if (subtype == MFVideoFormat_YUY2 ||
                    subtype == MFVideoFormat_NV12 || 
                    subtype == MFVideoFormat_YV12 || 
                    subtype == MFVideoFormat_RGB32) {
                    
                    pOutputMediaType = pOutputMediaTypeEnum;
                    printf("[C++ Renderer] Selected output format: %08X\n", subtype.Data1);
                    break;
                } else {
                    pOutputMediaTypeEnum->Release();
                }
                index++;
            }
        }

        // 如果没有找到合适的输出类型，使用默认设置
        if (!pOutputMediaType) {
            if (FAILED(MFCreateMediaType(&pOutputMediaType))) {
                std::cerr << "[C++ Renderer] Create output media type failed" << std::endl;
                return false;
            }

            if (FAILED(pOutputMediaType->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video))) {
                std::cerr << "[C++ Renderer] Set output media type failed" << std::endl;
                return false;
            }

            // 使用NV12格式，这是H264解码器常用的输出格式
            if (FAILED(pOutputMediaType->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12))) {
                std::cerr << "[C++ Renderer] Set output subtype failed" << std::endl;
                return false;
            }

            if (FAILED(MFSetAttributeSize(pOutputMediaType, MF_MT_FRAME_SIZE, width, height))) {
                std::cerr << "[C++ Renderer] Set output frame size failed" << std::endl;
                return false;
            }

            if (FAILED(MFSetAttributeRatio(pOutputMediaType, MF_MT_FRAME_RATE, 30, 1))) {
                std::cerr << "[C++ Renderer] Set output frame rate failed" << std::endl;
                return false;
            }

            if (FAILED(MFSetAttributeRatio(pOutputMediaType, MF_MT_PIXEL_ASPECT_RATIO, 1, 1))) {
                std::cerr << "[C++ Renderer] Set output pixel aspect ratio failed" << std::endl;
                return false;
            }
        }

        if (FAILED(pDecoder->SetOutputType(0, pOutputMediaType, 0))) {
            std::cerr << "[C++ Renderer] Set decoder output type failed" << std::endl;
            return false;
        }

        // 初始化解码器
        if (FAILED(pDecoder->ProcessMessage(MFT_MESSAGE_COMMAND_FLUSH, 0))) {
            std::cerr << "[C++ Renderer] Flush decoder failed" << std::endl;
            return false;
        }

        if (FAILED(pDecoder->ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0))) {
            std::cerr << "[C++ Renderer] Start stream processing failed" << std::endl;
            return false;
        }

        std::cout << "[C++ Renderer] Decoder initialized successfully" << std::endl;
        return true;
    }

    bool init_gdi() {
        hdc_window = GetDC(target_window);
        if (!hdc_window) {
            std::cerr << "[C++ Renderer] Get window DC failed" << std::endl;
            return false;
        }

        hdc_mem = CreateCompatibleDC(hdc_window);
        if (!hdc_mem) {
            std::cerr << "[C++ Renderer] Create memory DC failed" << std::endl;
            return false;
        }

        ZeroMemory(&bmi, sizeof(BITMAPINFO));
        bmi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
        bmi.bmiHeader.biWidth = width;
        bmi.bmiHeader.biHeight = -height;
        bmi.bmiHeader.biPlanes = 1;
        bmi.bmiHeader.biBitCount = 32;
        bmi.bmiHeader.biCompression = BI_RGB;

        hbitmap = CreateDIBSection(hdc_mem, &bmi, DIB_RGB_COLORS, (void**)&rgb_buffer, nullptr, 0);
        if (!hbitmap) {
            std::cerr << "[C++ Renderer] Create bitmap failed" << std::endl;
            return false;
        }

        SelectObject(hdc_mem, hbitmap);
        
        std::cout << "[C++ Renderer] GDI initialized successfully" << std::endl;
        return true;
    }

    void update_gdi() {
        if (hbitmap) {
            DeleteObject(hbitmap);
            hbitmap = nullptr;
        }
        
        // 不需要手动管理rgb_buffer，CreateDIBSection会分配内存
        bmi.bmiHeader.biWidth = width;
        bmi.bmiHeader.biHeight = -height;
        
        hbitmap = CreateDIBSection(hdc_mem, &bmi, DIB_RGB_COLORS, (void**)&rgb_buffer, nullptr, 0);
        if (hbitmap) {
            SelectObject(hdc_mem, hbitmap);
            printf("[C++ Renderer] GDI updated for resolution %dx%d\n", width, height);
        } else {
            printf("[C++ Renderer] CreateDIBSection failed: %d\n", GetLastError());
        }
    }

    void update_decoder_format() {
        if (pDecoder && pOutputMediaType) {
            printf("[C++ Renderer] Updating decoder output format for %dx%d\n", width, height);
            
            // 重置解码器
            pDecoder->ProcessMessage(MFT_MESSAGE_COMMAND_FLUSH, 0);
            pDecoder->ProcessMessage(MFT_MESSAGE_NOTIFY_END_STREAMING, 0);
            pDecoder->ProcessMessage(MFT_MESSAGE_NOTIFY_END_OF_STREAM, 0);
            
            // 释放旧的输出媒体类型
            if (pOutputMediaType) {
                pOutputMediaType->Release();
                pOutputMediaType = nullptr;
            }
            
            // 创建新的输出媒体类型
            if (FAILED(MFCreateMediaType(&pOutputMediaType))) {
                printf("[C++ Renderer] Create output media type failed\n");
                return;
            }
            
            if (FAILED(pOutputMediaType->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video))) {
                printf("[C++ Renderer] Set output media type failed\n");
                return;
            }
            
            // 使用NV12格式
            if (FAILED(pOutputMediaType->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12))) {
                printf("[C++ Renderer] Set output subtype failed\n");
                return;
            }
            
            // 设置新的帧大小
            if (FAILED(MFSetAttributeSize(pOutputMediaType, MF_MT_FRAME_SIZE, width, height))) {
                printf("[C++ Renderer] Update output frame size failed\n");
                return;
            }
            
            if (FAILED(MFSetAttributeRatio(pOutputMediaType, MF_MT_FRAME_RATE, 30, 1))) {
                printf("[C++ Renderer] Set output frame rate failed\n");
                return;
            }
            
            if (FAILED(MFSetAttributeRatio(pOutputMediaType, MF_MT_PIXEL_ASPECT_RATIO, 1, 1))) {
                printf("[C++ Renderer] Set output pixel aspect ratio failed\n");
                return;
            }
            
            // 重新设置输出类型
            if (FAILED(pDecoder->SetOutputType(0, pOutputMediaType, 0))) {
                printf("[C++ Renderer] Set decoder output type failed\n");
                return;
            }
            
            // 重新开始流式处理
            pDecoder->ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0);
            pDecoder->ProcessMessage(MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0);
            printf("[C++ Renderer] Decoder format updated successfully\n");
        }
    }

    void test_shared_memory() {
        // 测试共享内存写入（模拟Python端的写入格式）
        std::cout << "[C++ Renderer] Starting shared memory test..." << std::endl;
        
        // 写入测试数据
        uint32_t test_fid = 9999; // 测试帧ID
        uint32_t test_total_len = 8; // 测试总长度
        const char* test_data = "HELLO_TEST"; // 测试数据
        uint32_t test_data_len = 8; // 测试数据长度
        uint32_t total_size = 12 + test_data_len; // 总大小（12字节header + 数据长度）
        
        std::cout << "[C++ Renderer] Writing test data to shared memory..." << std::endl;
        
        // 写入数据大小（使用网络字节序）
        unsigned char* ptr = shm_buffer;
        *ptr++ = (total_size >> 24) & 0xFF;
        *ptr++ = (total_size >> 16) & 0xFF;
        *ptr++ = (total_size >> 8) & 0xFF;
        *ptr++ = total_size & 0xFF;
        
        // 写入帧ID（使用网络字节序）
        *ptr++ = (test_fid >> 24) & 0xFF;
        *ptr++ = (test_fid >> 16) & 0xFF;
        *ptr++ = (test_fid >> 8) & 0xFF;
        *ptr++ = test_fid & 0xFF;
        
        // 写入总长度（使用网络字节序）
        *ptr++ = (test_total_len >> 24) & 0xFF;
        *ptr++ = (test_total_len >> 16) & 0xFF;
        *ptr++ = (test_total_len >> 8) & 0xFF;
        *ptr++ = test_total_len & 0xFF;
        
        // 写入测试数据（从位置12开始）
        memcpy(ptr, test_data, test_data_len);
        
        std::cout << "[C++ Renderer] Test data written: total_size=" << total_size 
                  << ", fid=" << test_fid 
                  << ", total_len=" << test_total_len
                  << ", data_len=" << test_data_len << std::endl;
        
        // 读取验证（使用网络字节序）
        uint32_t read_data_size = read_uint32_network(shm_buffer);
        uint32_t read_fid = read_uint32_network(shm_buffer + 4);
        uint32_t read_total_len = read_uint32_network(shm_buffer + 8);
        uint32_t read_frame_size = read_data_size - 12;
        char read_test_data[9] = {0};
        memcpy(read_test_data, shm_buffer + 12, test_data_len);
        
        std::cout << "[C++ Renderer] Test data read back: data_size=" << read_data_size 
                  << ", fid=" << read_fid 
                  << ", total_len=" << read_total_len
                  << ", frame_size=" << read_frame_size 
                  << ", data='" << read_test_data << "'" << std::endl;
        
        if (read_data_size == total_size && 
            read_fid == test_fid && 
            read_total_len == test_total_len &&
            read_frame_size == test_data_len && 
            strcmp(read_test_data, test_data) == 0) {
            std::cout << "[C++ Renderer] Shared memory test PASSED!" << std::endl;
        } else {
            std::cerr << "[C++ Renderer] Shared memory test FAILED!" << std::endl;
            std::cerr << "[C++ Renderer] Expected: total_size=" << total_size 
                      << ", fid=" << test_fid 
                      << ", total_len=" << test_total_len
                      << ", data_len=" << test_data_len << std::endl;
        }
    }

    void render_loop() {
        int frame_count = 0;
        int last_fid = -1;
        int read_count = 0;
        bool test_done = false;
        bool decoder_initialized = false;
        
        printf("[C++ Renderer] Start render loop\n");
        
        while (running) {
            Sleep(1);
            
            // 运行一次共享内存测试
            if (!test_done) {
                test_shared_memory();
                test_done = true;
            }
            
            std::lock_guard<std::mutex> lock(frame_mutex);
            
            // 读取共享内存数据（使用正确的字节序）
            uint32_t data_size = read_uint32_network(shm_buffer);
            
            read_count++;
            if (read_count % 100 == 0) {
                printf("[C++ Renderer] Reading shared memory, data_size: %d\n", data_size);
            }
            
            // 数据验证
            if (data_size < 8 || data_size > SHM_SIZE) {
                // 不要频繁显示测试颜色，避免闪烁
                continue;
            }
            
            // 读取帧ID（使用正确的字节序）
            uint32_t fid = read_uint32_network(shm_buffer + 4);
            // 计算帧大小（总大小减去12字节的header：4字节data_size + 4字节fid + 4字节total_len）
            uint32_t frame_size = data_size - 12;
            
            // 验证帧大小
            if (frame_size < 0 || frame_size > MAX_FRAME_SIZE) {
                continue;
            }
            
            if (fid == 0xFFFFFFFF) {
                // 分辨率更新命令格式: 0xFFFFFFFF + width + height
                uint32_t new_width = read_uint32_network(shm_buffer + 8);
                uint32_t new_height = read_uint32_network(shm_buffer + 12);
                printf("[C++ Renderer] Received resolution update: %dx%d\n", new_width, new_height);
                update_resolution(new_width, new_height);
                decoder_initialized = false;
                continue;
            }
            
            if (fid == last_fid) {
                continue;
            }
            
            last_fid = fid;
            
            // 验证数据内容
            if (frame_size > 0) {
                // 快速检查H264数据有效性
                bool has_valid_start = false;
                for (int i = 0; i < frame_size - 3 && i < 10; i++) {
                    if (shm_buffer[12 + i] == 0 && shm_buffer[13 + i] == 0 && shm_buffer[14 + i] == 0 && shm_buffer[15 + i] == 1) {
                        has_valid_start = true;
                        break;
                    }
                }
                if (!has_valid_start) {
                    continue;
                }
            }
            
            // 检查是否是关键帧（I帧）
            bool is_i_frame = false;
            if (frame_size > 4) {
                // 查找H264起始码
                int start_code_pos = -1;
                for (int i = 0; i < frame_size - 3; i++) {
                    if (shm_buffer[12 + i] == 0 && shm_buffer[13 + i] == 0 && shm_buffer[14 + i] == 0 && shm_buffer[15 + i] == 1) {
                        start_code_pos = i;
                        break;
                    }
                }
                
                if (start_code_pos != -1 && start_code_pos + 4 < frame_size) {
                    unsigned char nal_type = shm_buffer[12 + start_code_pos + 4] & 0x1F;
                    if (nal_type == 5) {
                        is_i_frame = true;
                    }
                }
            }
            
            // 如果是I帧或者解码器未初始化，重置解码器
            if (is_i_frame || !decoder_initialized) {
                pDecoder->ProcessMessage(MFT_MESSAGE_COMMAND_FLUSH, 0);
                pDecoder->ProcessMessage(MFT_MESSAGE_NOTIFY_END_STREAMING, 0);
                pDecoder->ProcessMessage(MFT_MESSAGE_NOTIFY_END_OF_STREAM, 0);
                pDecoder->ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0);
                pDecoder->ProcessMessage(MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0);
                decoder_initialized = true;
            }
            
            // 使用Media Foundation解码H264数据
            if (decode_h264(shm_buffer + 12, frame_size)) {
                // 执行渲染
                RECT rect;
                if (GetClientRect(target_window, &rect)) {
                    int client_width = rect.right - rect.left;
                    int client_height = rect.bottom - rect.top;
                    
                    // 只有当客户端区域有效时才渲染
                    if (client_width > 0 && client_height > 0 && rgb_buffer) {
                        // 确保使用窗口的实际大小进行缩放
                        // 这样无论是I帧还是P帧，都会根据窗口实际大小进行渲染
                        BOOL blt_result = StretchBlt(
                            hdc_window, 
                            0, 0, 
                            client_width, client_height,
                            hdc_mem, 
                            0, 0, 
                            width, height, 
                            SRCCOPY
                        );
                    }
                }
                
                frame_count++;
            }
        }
        
        printf("[C++ Renderer] Render loop stopped\n");
    }
    
    void fill_test_color() {
        // 填充测试颜色（蓝色）以便调试
        for (int i = 0; i < height; i++) {
            for (int j = 0; j < width; j++) {
                int rgb_index = (i * width + j) * 4;
                rgb_buffer[rgb_index] = 255;     // B
                rgb_buffer[rgb_index + 1] = 0; // G
                rgb_buffer[rgb_index + 2] = 0; // R
                rgb_buffer[rgb_index + 3] = 255; // A
            }
        }
        
        // 渲染测试颜色
        RECT rect;
        GetClientRect(target_window, &rect);
        StretchBlt(
            hdc_window, 
            0, 0, 
            rect.right - rect.left, rect.bottom - rect.top,
            hdc_mem, 
            0, 0, 
            width, height, 
            SRCCOPY
        );
        
        std::cout << "[C++ Renderer] Filled test color (blue)" << std::endl;
    }

    bool decode_h264(unsigned char* h264_data, int data_size) {
        // 检查输入参数
        if (!h264_data || data_size <= 0) {
            return false;
        }
        
        // 检查是否包含H264 NAL单元起始码
        bool has_start_code = false;
        int start_code_pos = -1;
        for (int i = 0; i < data_size - 3; i++) {
            if (h264_data[i] == 0 && h264_data[i+1] == 0 && h264_data[i+2] == 0 && h264_data[i+3] == 1) {
                has_start_code = true;
                start_code_pos = i;
                break;
            }
        }
        
        if (!has_start_code) {
            return false;
        }
        
        // 检查是否是I帧
        bool is_i_frame = false;
        if (start_code_pos + 4 < data_size) {
            unsigned char nal_type = h264_data[start_code_pos + 4] & 0x1F;
            if (nal_type == 5) {
                is_i_frame = true;
                // 重置解码器状态以处理I帧
                pDecoder->ProcessMessage(MFT_MESSAGE_COMMAND_FLUSH, 0);
                pDecoder->ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0);
                printf("[C++ Renderer] Processing I-frame, reset decoder\n");
            }
        }
        
        // 创建媒体缓冲区
        IMFMediaBuffer* pMediaBuffer = nullptr;
        if (FAILED(MFCreateMemoryBuffer(data_size, &pMediaBuffer))) {
            return false;
        }

        // 锁定缓冲区并复制数据
        BYTE* buffer_data;
        DWORD max_length, current_length;
        
        if (FAILED(pMediaBuffer->Lock(&buffer_data, &max_length, &current_length))) {
            pMediaBuffer->Release();
            return false;
        }

        memcpy(buffer_data, h264_data, data_size);
        
        if (FAILED(pMediaBuffer->SetCurrentLength(data_size))) {
            pMediaBuffer->Unlock();
            pMediaBuffer->Release();
            return false;
        }

        pMediaBuffer->Unlock();

        // 创建媒体样本
        IMFSample* pSample = nullptr;
        if (FAILED(MFCreateSample(&pSample))) {
            pMediaBuffer->Release();
            return false;
        }

        if (FAILED(pSample->AddBuffer(pMediaBuffer))) {
            pSample->Release();
            pMediaBuffer->Release();
            return false;
        }

        // 处理输入数据
        DWORD stream_flags = 0;
        HRESULT hr_input = pDecoder->ProcessInput(0, pSample, 0);
        if (FAILED(hr_input)) {
            // 尝试刷新解码器后重试
            pDecoder->ProcessMessage(MFT_MESSAGE_COMMAND_FLUSH, 0);
            pDecoder->ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0);
            
            hr_input = pDecoder->ProcessInput(0, pSample, 0);
            if (FAILED(hr_input)) {
                pSample->Release();
                pMediaBuffer->Release();
                return false;
            }
        }

        // 处理输出数据
        MFT_OUTPUT_DATA_BUFFER outputData;
        DWORD status = 0;

        ZeroMemory(&outputData, sizeof(MFT_OUTPUT_DATA_BUFFER));

        // 分配输出样本
        if (FAILED(MFCreateSample(&outputData.pSample))) {
            pSample->Release();
            pMediaBuffer->Release();
            return false;
        }

        // 为输出样本创建媒体缓冲区
        IMFMediaBuffer* pOutputBuffer = nullptr;
        // 估计输出缓冲区大小（基于分辨率计算，使用较大的缓冲区以确保足够）
        DWORD output_buffer_size = width * height * 4; // 使用RGB32大小以确保足够
        if (FAILED(MFCreateMemoryBuffer(output_buffer_size, &pOutputBuffer))) {
            outputData.pSample->Release();
            pSample->Release();
            pMediaBuffer->Release();
            return false;
        }

        if (FAILED(outputData.pSample->AddBuffer(pOutputBuffer))) {
            pOutputBuffer->Release();
            outputData.pSample->Release();
            pSample->Release();
            pMediaBuffer->Release();
            return false;
        }
        pOutputBuffer->Release(); // 释放引用，样本会持有它

        // 尝试多次获取输出
        bool output_success = false;
        for (int i = 0; i < 5; i++) {
            // 处理输出
            HRESULT hr_output = pDecoder->ProcessOutput(0, 1, &outputData, &status);
            if (SUCCEEDED(hr_output)) {
                // 锁定输出缓冲区
                DWORD count = 0;
                
                if (SUCCEEDED(outputData.pSample->GetBufferCount(&count)) && count > 0) {
                    if (SUCCEEDED(outputData.pSample->GetBufferByIndex(0, &pOutputBuffer))) {
                        BYTE* output_data = nullptr;
                        DWORD output_length = 0;
                        
                        if (SUCCEEDED(pOutputBuffer->Lock(&output_data, nullptr, &output_length))) {
                            // 检查输出格式
                            GUID output_subtype;
                            if (SUCCEEDED(pOutputMediaType->GetGUID(MF_MT_SUBTYPE, &output_subtype))) {
                                // 调试：打印输出格式
                                static int format_print_count = 0;
                                if (format_print_count < 5) {
                                    printf("[C++ Renderer] Output format: %08X-%04X-%04X-%02X%02X%02X%02X%02X%02X%02X%02X\n",
                                           output_subtype.Data1, output_subtype.Data2, output_subtype.Data3,
                                           output_subtype.Data4[0], output_subtype.Data4[1], output_subtype.Data4[2], output_subtype.Data4[3],
                                           output_subtype.Data4[4], output_subtype.Data4[5], output_subtype.Data4[6], output_subtype.Data4[7]);
                                    printf("[C++ Renderer] Output length: %d bytes\n", output_length);
                                    format_print_count++;
                                }
                                // 使用正确的格式进行转换
                                convert_to_rgb32(output_data, output_length, output_subtype);
                                output_success = true;
                            }
                            pOutputBuffer->Unlock();
                        }
                        pOutputBuffer->Release();
                    }
                }
                break;
            } else if (hr_output == MF_E_TRANSFORM_NEED_MORE_INPUT) {
                // 需要更多输入数据，退出循环
                break;
            } else if (hr_output == MF_E_TRANSFORM_STREAM_CHANGE) {
                // 流格式改变，重置解码器
                pDecoder->ProcessMessage(MFT_MESSAGE_COMMAND_FLUSH, 0);
                pDecoder->ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0);
            } else {
                // 其他错误，尝试重置解码器
                pDecoder->ProcessMessage(MFT_MESSAGE_COMMAND_FLUSH, 0);
                pDecoder->ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0);
            }
        }

        // 释放资源
        if (outputData.pSample) {
            outputData.pSample->Release();
        }

        pSample->Release();
        pMediaBuffer->Release();

        return output_success;
    }

    void convert_to_rgb32(unsigned char* input_data, int input_size, const GUID& subtype) {
        if (input_data == nullptr || rgb_buffer == nullptr) {
            return;
        }
        
        // 清除RGB缓冲区
        memset(rgb_buffer, 0, width * height * 4);
        
        if (subtype == MFVideoFormat_YUY2) {
            // YUY2格式：每个像素对（2个像素）占用4个字节：Y0 U Y1 V
            int yuy2_size = width * height * 2;
            
            if (input_size >= yuy2_size) {
                for (int i = 0; i < height; i++) {
                    for (int j = 0; j < width; j += 2) {
                        // 每次处理2个像素
                        int yuy2_index = i * width * 2 + j * 2;
                        
                        if (yuy2_index + 3 < input_size) {
                            int y0 = input_data[yuy2_index];
                            int u = input_data[yuy2_index + 1];
                            int y1 = input_data[yuy2_index + 2];
                            int v = input_data[yuy2_index + 3];
                            
                            // 第一个像素
                            int r0 = (298 * (y0 - 16) + 409 * (v - 128) + 128) >> 8;
                            int g0 = (298 * (y0 - 16) - 100 * (u - 128) - 208 * (v - 128) + 128) >> 8;
                            int b0 = (298 * (y0 - 16) + 516 * (u - 128) + 128) >> 8;
                            
                            r0 = (r0 < 0) ? 0 : (r0 > 255) ? 255 : r0;
                            g0 = (g0 < 0) ? 0 : (g0 > 255) ? 255 : g0;
                            b0 = (b0 < 0) ? 0 : (b0 > 255) ? 255 : b0;
                            
                            int rgb_index0 = (i * width + j) * 4;
                            rgb_buffer[rgb_index0] = (unsigned char)b0;
                            rgb_buffer[rgb_index0 + 1] = (unsigned char)g0;
                            rgb_buffer[rgb_index0 + 2] = (unsigned char)r0;
                            rgb_buffer[rgb_index0 + 3] = 255;
                            
                            // 第二个像素（如果存在）
                            if (j + 1 < width) {
                                int r1 = (298 * (y1 - 16) + 409 * (v - 128) + 128) >> 8;
                                int g1 = (298 * (y1 - 16) - 100 * (u - 128) - 208 * (v - 128) + 128) >> 8;
                                int b1 = (298 * (y1 - 16) + 516 * (u - 128) + 128) >> 8;
                                
                                r1 = (r1 < 0) ? 0 : (r1 > 255) ? 255 : r1;
                                g1 = (g1 < 0) ? 0 : (g1 > 255) ? 255 : g1;
                                b1 = (b1 < 0) ? 0 : (b1 > 255) ? 255 : b1;
                                
                                int rgb_index1 = (i * width + j + 1) * 4;
                                rgb_buffer[rgb_index1] = (unsigned char)b1;
                                rgb_buffer[rgb_index1 + 1] = (unsigned char)g1;
                                rgb_buffer[rgb_index1 + 2] = (unsigned char)r1;
                                rgb_buffer[rgb_index1 + 3] = 255;
                            }
                        }
                    }
                }
            }
        } else if (subtype == MFVideoFormat_NV12) {
            // 计算实际的NV12大小（考虑内存对齐）
            int aligned_width = (width + 15) & ~15;
            int aligned_height = (height + 1) & ~1;
            int y_size = aligned_width * aligned_height;
            int uv_size = y_size / 2;
            int total_size = y_size + uv_size;
            
            // 确保输入数据足够大
            if (input_size >= total_size) {
                unsigned char* y_plane = input_data;
                unsigned char* uv_plane = input_data + y_size;
                
                // 遍历每一行
                for (int i = 0; i < height; i++) {
                    // 遍历每一列
                    for (int j = 0; j < width; j++) {
                        // 计算Y平面索引（使用对齐宽度）
                        int y_index = i * aligned_width + j;
                        if (y_index < y_size) {
                            int y = y_plane[y_index];
                            
                            // 计算UV平面索引（NV12格式：UV数据交错存储，每个2x2像素块共享一组UV值）
                            int uv_row = i / 2;
                            int uv_col = j / 2;
                            int uv_index = uv_row * aligned_width + uv_col * 2;
                            
                            // 确保UV索引在范围内
                            if (uv_index + 1 < uv_size) {
                                int u = uv_plane[uv_index];
                                int v = uv_plane[uv_index + 1];
                                
                                // YUV to RGB conversion (标准BT.601，使用整数计算避免浮点误差)
                                int r = (298 * (y - 16) + 409 * (v - 128) + 128) >> 8;
                                int g = (298 * (y - 16) - 100 * (u - 128) - 208 * (v - 128) + 128) >> 8;
                                int b = (298 * (y - 16) + 516 * (u - 128) + 128) >> 8;
                                
                                // Clamp values to 0-255
                                r = (r < 0) ? 0 : (r > 255) ? 255 : r;
                                g = (g < 0) ? 0 : (g > 255) ? 255 : g;
                                b = (b < 0) ? 0 : (b > 255) ? 255 : b;
                                
                                // Store in RGB32 buffer (BGRA format)
                                int rgb_index = (i * width + j) * 4;
                                rgb_buffer[rgb_index] = (unsigned char)b;     // B
                                rgb_buffer[rgb_index + 1] = (unsigned char)g; // G
                                rgb_buffer[rgb_index + 2] = (unsigned char)r; // R
                                rgb_buffer[rgb_index + 3] = 255; // A
                            }
                        }
                    }
                }
            }
        } else if (subtype == MFVideoFormat_RGB32) {
            if (input_size >= width * height * 4) {
                memcpy(rgb_buffer, input_data, width * height * 4);
            }
        } else if (subtype == MFVideoFormat_YV12) {
            // 处理YV12格式
            int y_size = width * height;
            int u_size = y_size / 4;
            int v_size = y_size / 4;
            int total_size = y_size + u_size + v_size;
            
            if (input_size >= total_size) {
                unsigned char* y_plane = input_data;
                unsigned char* v_plane = input_data + y_size;
                unsigned char* u_plane = input_data + y_size + v_size;
                
                for (int i = 0; i < height; i++) {
                    for (int j = 0; j < width; j++) {
                        int y_index = i * width + j;
                        if (y_index < y_size) {
                            int y = y_plane[y_index];
                            
                            int uv_row = i / 2;
                            int uv_col = j / 2;
                            int uv_index = uv_row * (width / 2);
                            
                            if (uv_index < u_size) {
                                int u = u_plane[uv_index];
                                int v = v_plane[uv_index];
                                
                                // YUV to RGB conversion
                                int r = (298 * (y - 16) + 409 * (v - 128) + 128) >> 8;
                                int g = (298 * (y - 16) - 100 * (u - 128) - 208 * (v - 128) + 128) >> 8;
                                int b = (298 * (y - 16) + 516 * (u - 128) + 128) >> 8;
                                
                                // Clamp values to 0-255
                                r = (r < 0) ? 0 : (r > 255) ? 255 : r;
                                g = (g < 0) ? 0 : (g > 255) ? 255 : g;
                                b = (b < 0) ? 0 : (b > 255) ? 255 : b;
                                
                                int rgb_index = (i * width + j) * 4;
                                rgb_buffer[rgb_index] = (unsigned char)b;     // B
                                rgb_buffer[rgb_index + 1] = (unsigned char)g; // G
                                rgb_buffer[rgb_index + 2] = (unsigned char)r; // R
                                rgb_buffer[rgb_index + 3] = 255; // A
                            }
                        }
                    }
                }
            }
        } else {
            // 对于其他格式，尝试直接复制（可能是RGB格式）
            if (input_size >= width * height * 4) {
                memcpy(rgb_buffer, input_data, width * height * 4);
            }
        }
    }

    void cleanup() {
        if (pDecoder) {
            pDecoder->ProcessMessage(MFT_MESSAGE_NOTIFY_END_STREAMING, 0);
            pDecoder->ProcessMessage(MFT_MESSAGE_COMMAND_FLUSH, 0);
            pDecoder->Release();
        }

        if (pInputMediaType) {
            pInputMediaType->Release();
        }

        if (pOutputMediaType) {
            pOutputMediaType->Release();
        }

        if (hbitmap) {
            DeleteObject(hbitmap);
        }
        
        if (hdc_mem) {
            DeleteDC(hdc_mem);
        }
        
        if (hdc_window) {
            ReleaseDC(target_window, hdc_window);
        }
        
        if (rgb_buffer) {
            delete[] rgb_buffer;
        }
        
        if (shm_buffer) {
            UnmapViewOfFile(shm_buffer);
        }
        
        if (shm_handle) {
            CloseHandle(shm_handle);
        }

        MFShutdown();
        CoUninitialize();
        
        std::cout << "[C++ Renderer] Cleanup completed" << std::endl;
    }
};

int main(int argc, char* argv[]) {
    if (argc < 5) {
        std::cerr << "用法: " << argv[0] << " <窗口句柄> <共享内存名称> <宽度> <高度>" << std::endl;
        return 1;
    }

    HWND hwnd = (HWND)std::stoull(argv[1]);
    std::string shm_name = argv[2];
    int width = std::stoi(argv[3]);
    int height = std::stoi(argv[4]);

    std::cout << "[C++ Renderer] Startup parameters:" << std::endl;
        std::cout << "[C++ Renderer]   Window handle: " << hwnd << std::endl;
        std::cout << "[C++ Renderer]   Shared memory: " << shm_name << std::endl;
        std::cout << "[C++ Renderer]   Resolution: " << width << "x" << height << std::endl;

    H264Renderer renderer(hwnd, shm_name, width, height);
    
    if (!renderer.init()) {
        std::cerr << "[C++ Renderer] Initialization failed" << std::endl;
        return 1;
    }

    renderer.start();

    std::cout << "[C++ Renderer] Press Enter to exit..." << std::endl;
    std::cin.get();

    renderer.stop();
    
    return 0;
}