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
        
        std::cout << "[C++ Renderer] Update resolution: " << new_width << "x" << new_height << std::endl;
        
        width = new_width;
        height = new_height;
        
        update_gdi();
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

        while (SUCCEEDED(hr)) {
            hr = pDecoder->GetOutputAvailableType(0, index, &pOutputMediaTypeEnum);
            if (SUCCEEDED(hr)) {
                GUID subtype = GUID_NULL;
                pOutputMediaTypeEnum->GetGUID(MF_MT_SUBTYPE, &subtype);
                
                // 尝试使用YUV或RGB格式
                if (subtype == MFVideoFormat_NV12 || 
                    subtype == MFVideoFormat_YV12 || 
                    subtype == MFVideoFormat_RGB32) {
                    
                    pOutputMediaType = pOutputMediaTypeEnum;
                    std::cout << "[C++ Renderer] Found output format: " << subtype.Data1 << std::endl;
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
        }
        
        rgb_buffer_size = width * height * 4;
        delete[] rgb_buffer;
        rgb_buffer = new unsigned char[rgb_buffer_size];
        
        bmi.bmiHeader.biWidth = width;
        bmi.bmiHeader.biHeight = -height;
        
        hbitmap = CreateDIBSection(hdc_mem, &bmi, DIB_RGB_COLORS, (void**)&rgb_buffer, nullptr, 0);
        SelectObject(hdc_mem, hbitmap);
    }

    void render_loop() {
        int frame_count = 0;
        int last_fid = -1;
        int read_count = 0;
        
        std::cout << "[C++ Renderer] Start render loop" << std::endl;
        
        while (running) {
            Sleep(1);
            
            std::lock_guard<std::mutex> lock(frame_mutex);
            
            // 读取共享内存数据
            uint32_t data_size;
            memcpy(&data_size, shm_buffer, 4);
            
            read_count++;
            if (read_count % 100 == 0) {
                std::cout << "[C++ Renderer] Reading shared memory, data_size: " << data_size << std::endl;
            }
            
            if (data_size < 8 || data_size > SHM_SIZE) {
                continue;
            }
            
            uint32_t fid;
            uint32_t frame_size;
            memcpy(&fid, shm_buffer + 4, 4);
            memcpy(&frame_size, shm_buffer + 8, 4);
            
            std::cout << "[C++ Renderer] Read frame: fid=" << fid << ", size=" << frame_size << " bytes" << std::endl;
            
            if (fid == 0xFFFFFFFF) {
                uint32_t new_width, new_height;
                memcpy(&new_width, shm_buffer + 4, 4);
                memcpy(&new_height, shm_buffer + 8, 4);
                update_resolution(new_width, new_height);
                continue;
            }
            
            if (fid == last_fid) {
                std::cout << "[C++ Renderer] Skipping duplicate frame: " << fid << std::endl;
                continue;
            }
            
            last_fid = fid;
            
            if (frame_size > MAX_FRAME_SIZE) {
                std::cerr << "[C++ Renderer] Frame size too large: " << frame_size << std::endl;
                continue;
            }
            
            // 验证数据内容
            if (frame_size > 0) {
                unsigned char first_byte = shm_buffer[12];
                unsigned char second_byte = shm_buffer[13];
                std::cout << "[C++ Renderer] First 2 bytes of frame: 0x" << std::hex << (int)first_byte << " 0x" << (int)second_byte << std::endl;
            }
            
            // 使用Media Foundation解码H264数据
            std::cout << "[C++ Renderer] Start decoding frame: " << fid << std::endl;
            if (decode_h264(shm_buffer + 12, frame_size)) {
                // 验证RGB缓冲区内容
                if (rgb_buffer) {
                    int test_index = 0;
                    unsigned char r = rgb_buffer[test_index + 2];
                    unsigned char g = rgb_buffer[test_index + 1];
                    unsigned char b = rgb_buffer[test_index];
                    std::cout << "[C++ Renderer] RGB buffer content at (0,0): R=" << (int)r << ", G=" << (int)g << ", B=" << (int)b << std::endl;
                }
                
                // 执行渲染
                RECT rect;
                GetClientRect(target_window, &rect);
                std::cout << "[C++ Renderer] Window client area: " << (rect.right - rect.left) << "x" << (rect.bottom - rect.top) << std::endl;
                
                BOOL blt_result = StretchBlt(
                    hdc_window, 
                    0, 0, 
                    rect.right - rect.left, rect.bottom - rect.top,
                    hdc_mem, 
                    0, 0, 
                    width, height, 
                    SRCCOPY
                );
                
                std::cout << "[C++ Renderer] StretchBlt result: " << (blt_result ? "SUCCESS" : "FAILED") << std::endl;
                
                frame_count++;
                std::cout << "[C++ Renderer] Rendered frame " << frame_count << ": " << fid << std::endl;
            }
        }
        
        std::cout << "[C++ Renderer] Render loop stopped" << std::endl;
    }

    bool decode_h264(unsigned char* h264_data, int data_size) {
        std::cout << "[C++ Renderer] Decode H264 frame, size: " << data_size << " bytes" << std::endl;
        
        // 创建媒体缓冲区
        if (FAILED(MFCreateMemoryBuffer(data_size, &pMediaBuffer))) {
            std::cerr << "[C++ Renderer] Create media buffer failed" << std::endl;
            return false;
        }

        // 锁定缓冲区并复制数据
        BYTE* buffer_data;
        DWORD max_length, current_length;
        
        if (FAILED(pMediaBuffer->Lock(&buffer_data, &max_length, &current_length))) {
            std::cerr << "[C++ Renderer] Lock media buffer failed" << std::endl;
            pMediaBuffer->Release();
            return false;
        }

        memcpy(buffer_data, h264_data, data_size);
        
        if (FAILED(pMediaBuffer->SetCurrentLength(data_size))) {
            std::cerr << "[C++ Renderer] Set media buffer length failed" << std::endl;
            pMediaBuffer->Unlock();
            pMediaBuffer->Release();
            return false;
        }

        pMediaBuffer->Unlock();
        std::cout << "[C++ Renderer] Media buffer prepared" << std::endl;

        // 创建媒体样本
        if (FAILED(MFCreateSample(&pSample))) {
            std::cerr << "[C++ Renderer] Create media sample failed" << std::endl;
            pMediaBuffer->Release();
            return false;
        }

        if (FAILED(pSample->AddBuffer(pMediaBuffer))) {
            std::cerr << "[C++ Renderer] Add media buffer failed" << std::endl;
            pSample->Release();
            pMediaBuffer->Release();
            return false;
        }

        // 处理输入数据
        std::cout << "[C++ Renderer] Process input data" << std::endl;
        DWORD stream_flags = 0;
        HRESULT hr_input = pDecoder->ProcessInput(0, pSample, 0);
        if (FAILED(hr_input)) {
            std::cerr << "[C++ Renderer] Process input data failed, hr: " << hr_input << std::endl;
            pSample->Release();
            pMediaBuffer->Release();
            return false;
        }
        std::cout << "[C++ Renderer] Process input data success" << std::endl;

        // 处理输出数据
        MFT_OUTPUT_DATA_BUFFER outputData;
        DWORD status = 0;

        ZeroMemory(&outputData, sizeof(MFT_OUTPUT_DATA_BUFFER));

        // 分配输出样本
        if (FAILED(MFCreateSample(&outputData.pSample))) {
            std::cerr << "[C++ Renderer] Create output sample failed" << std::endl;
            pSample->Release();
            pMediaBuffer->Release();
            return false;
        }

        // 处理输出
        std::cout << "[C++ Renderer] Process output data" << std::endl;
        HRESULT hr_output = pDecoder->ProcessOutput(0, 1, &outputData, &status);
        if (SUCCEEDED(hr_output)) {
            std::cout << "[C++ Renderer] Process output data success, status: " << status << std::endl;
            
            // 锁定输出缓冲区
            IMFMediaBuffer* pOutputBuffer = nullptr;
            DWORD count = 0;
            
            if (SUCCEEDED(outputData.pSample->GetBufferCount(&count)) && count > 0) {
                std::cout << "[C++ Renderer] Output buffer count: " << count << std::endl;
                
                if (SUCCEEDED(outputData.pSample->GetBufferByIndex(0, &pOutputBuffer))) {
                    BYTE* output_data = nullptr;
                    DWORD output_length = 0;
                    
                    if (SUCCEEDED(pOutputBuffer->Lock(&output_data, nullptr, &output_length))) {
                        std::cout << "[C++ Renderer] Output buffer length: " << output_length << " bytes" << std::endl;
                        
                        // 使用默认的NV12格式进行转换
                        // 因为我们在初始化时已经设置了解码器使用NV12或RGB32格式
                        convert_to_rgb32(output_data, output_length, MFVideoFormat_NV12);
                        pOutputBuffer->Unlock();
                    }
                    pOutputBuffer->Release();
                }
            }
        } else {
            std::cerr << "[C++ Renderer] Process output data failed, hr: " << hr_output << std::endl;
        }

        // 释放资源
        if (outputData.pSample) {
            outputData.pSample->Release();
        }

        pSample->Release();
        pMediaBuffer->Release();

        std::cout << "[C++ Renderer] Decode completed" << std::endl;
        return true;
    }

    void convert_to_rgb32(unsigned char* input_data, int input_size, const GUID& subtype) {
        std::cout << "[C++ Renderer] Convert to RGB32, input size: " << input_size << " bytes" << std::endl;
        
        if (subtype == MFVideoFormat_NV12) {
            int y_size = width * height;
            int uv_size = y_size / 2;
            int total_size = y_size + uv_size;
            
            std::cout << "[C++ Renderer] NV12 conversion: y_size=" << y_size << ", uv_size=" << uv_size << ", total_size=" << total_size << std::endl;
            
            if (input_size >= total_size) {
                unsigned char* y_plane = input_data;
                unsigned char* uv_plane = input_data + y_size;
                
                std::cout << "[C++ Renderer] Start NV12 to RGB conversion" << std::endl;
                
                for (int i = 0; i < height; i++) {
                    for (int j = 0; j < width; j++) {
                        int y = y_plane[i * width + j];
                        int uv_index = (i / 2) * width + (j / 2) * 2;
                        int u = uv_plane[uv_index];
                        int v = uv_plane[uv_index + 1];
                        
                        // YUV to RGB conversion
                        int c = y - 16;
                        int d = u - 128;
                        int e = v - 128;
                        
                        int r = (298 * c + 409 * e + 128) >> 8;
                        int g = (298 * c - 100 * d - 208 * e + 128) >> 8;
                        int b = (298 * c + 516 * d + 128) >> 8;
                        
                        // Clamp values
                        r = (r < 0) ? 0 : (r > 255) ? 255 : r;
                        g = (g < 0) ? 0 : (g > 255) ? 255 : g;
                        b = (b < 0) ? 0 : (b > 255) ? 255 : b;
                        
                        // Store in RGB32 buffer (BGRA format)
                        int rgb_index = (i * width + j) * 4;
                        rgb_buffer[rgb_index] = b;     // B
                        rgb_buffer[rgb_index + 1] = g; // G
                        rgb_buffer[rgb_index + 2] = r; // R
                        rgb_buffer[rgb_index + 3] = 255; // A
                    }
                }
                
                std::cout << "[C++ Renderer] NV12 to RGB conversion completed" << std::endl;
            } else {
                std::cerr << "[C++ Renderer] Input size too small for NV12 conversion" << std::endl;
                
                // 填充测试颜色（红色）以便调试
                for (int i = 0; i < height; i++) {
                    for (int j = 0; j < width; j++) {
                        int rgb_index = (i * width + j) * 4;
                        rgb_buffer[rgb_index] = 0;     // B
                        rgb_buffer[rgb_index + 1] = 0; // G
                        rgb_buffer[rgb_index + 2] = 255; // R
                        rgb_buffer[rgb_index + 3] = 255; // A
                    }
                }
                std::cout << "[C++ Renderer] Filled buffer with test color (red)" << std::endl;
            }
        } else {
            std::cout << "[C++ Renderer] Unknown format, filling with test color" << std::endl;
            
            // 填充测试颜色（绿色）以便调试
            for (int i = 0; i < height; i++) {
                for (int j = 0; j < width; j++) {
                    int rgb_index = (i * width + j) * 4;
                    rgb_buffer[rgb_index] = 0;     // B
                    rgb_buffer[rgb_index + 1] = 255; // G
                    rgb_buffer[rgb_index + 2] = 0; // R
                    rgb_buffer[rgb_index + 3] = 255; // A
                }
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