#include <windows.h>
#include <iostream>
#include <string>
#include <thread>
#include <mutex>
#include <atomic>
#include <vector>
#include <cstring>

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/imgutils.h>
#include <libswscale/swscale.h>
}

const int SHM_SIZE = 10 * 1024 * 1024;
const int MAX_FRAME_SIZE = 8 * 1024 * 1024;

class H264Renderer {
private:
    HWND target_window;
    HANDLE shm_handle;
    unsigned char* shm_buffer;
    std::string shm_name;
    
    AVCodecContext* codec_ctx;
    AVFrame* frame;
    AVFrame* frame_rgb;
    SwsContext* sws_ctx;
    
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

public:
    H264Renderer(HWND hwnd, const std::string& shm_name, int w, int h)
        : target_window(hwnd), shm_name(shm_name), width(w), height(h),
          running(false), shm_handle(nullptr), shm_buffer(nullptr),
          codec_ctx(nullptr), frame(nullptr), frame_rgb(nullptr),
          sws_ctx(nullptr), hdc_window(nullptr), hdc_mem(nullptr),
          hbitmap(nullptr), rgb_buffer(nullptr) {
        
        std::cout << "[C++渲染] 初始化渲染器" << std::endl;
        std::cout << "[C++渲染] 窗口句柄: " << target_window << std::endl;
        std::cout << "[C++渲染] 共享内存: " << shm_name << std::endl;
        std::cout << "[C++渲染] 分辨率: " << width << "x" << height << std::endl;
    }

    ~H264Renderer() {
        stop();
        cleanup();
    }

    bool init() {
        if (!init_shared_memory()) {
            std::cerr << "[C++渲染] 共享内存初始化失败" << std::endl;
            return false;
        }

        if (!init_ffmpeg()) {
            std::cerr << "[C++渲染] FFmpeg初始化失败" << std::endl;
            return false;
        }

        if (!init_gdi()) {
            std::cerr << "[C++渲染] GDI初始化失败" << std::endl;
            return false;
        }

        running = true;
        return true;
    }

    void start() {
        std::cout << "[C++渲染] 启动渲染线程" << std::endl;
        std::thread render_thread(&H264Renderer::render_loop, this);
        render_thread.detach();
    }

    void stop() {
        running = false;
    }

    void update_resolution(int new_width, int new_height) {
        std::lock_guard<std::mutex> lock(frame_mutex);
        
        std::cout << "[C++渲染] 更新分辨率: " << new_width << "x" << new_height << std::endl;
        
        width = new_width;
        height = new_height;
        
        update_gdi();
        update_sws_context();
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
            std::cerr << "[C++渲染] 创建共享内存失败: " << GetLastError() << std::endl;
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
            std::cerr << "[C++渲染] 映射共享内存失败: " << GetLastError() << std::endl;
            CloseHandle(shm_handle);
            return false;
        }

        std::cout << "[C++渲染] 共享内存初始化成功" << std::endl;
        return true;
    }

    bool init_ffmpeg() {
        avcodec_register_all();
        
        AVCodec* codec = avcodec_find_decoder(AV_CODEC_ID_H264);
        if (!codec) {
            std::cerr << "[C++渲染] 找不到H264解码器" << std::endl;
            return false;
        }

        codec_ctx = avcodec_alloc_context3(codec);
        if (!codec_ctx) {
            std::cerr << "[C++渲染] 分配解码器上下文失败" << std::endl;
            return false;
        }

        if (avcodec_open2(codec_ctx, codec, nullptr) < 0) {
            std::cerr << "[C++渲染] 打开解码器失败" << std::endl;
            return false;
        }

        frame = av_frame_alloc();
        frame_rgb = av_frame_alloc();
        
        if (!frame || !frame_rgb) {
            std::cerr << "[C++渲染] 分配帧失败" << std::endl;
            return false;
        }

        rgb_buffer_size = width * height * 4;
        rgb_buffer = new unsigned char[rgb_buffer_size];
        
        std::cout << "[C++渲染] FFmpeg初始化成功" << std::endl;
        return true;
    }

    bool init_gdi() {
        hdc_window = GetDC(target_window);
        if (!hdc_window) {
            std::cerr << "[C++渲染] 获取窗口DC失败" << std::endl;
            return false;
        }

        hdc_mem = CreateCompatibleDC(hdc_window);
        if (!hdc_mem) {
            std::cerr << "[C++渲染] 创建内存DC失败" << std::endl;
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
            std::cerr << "[C++渲染] 创建位图失败" << std::endl;
            return false;
        }

        SelectObject(hdc_mem, hbitmap);
        
        std::cout << "[C++渲染] GDI初始化成功" << std::endl;
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

    void update_sws_context() {
        if (sws_ctx) {
            sws_freeContext(sws_ctx);
        }
        
        sws_ctx = sws_getContext(
            codec_ctx->width, codec_ctx->height, AV_PIX_FMT_YUV420P,
            width, height, AV_PIX_FMT_BGRA,
            SWS_BILINEAR, nullptr, nullptr, nullptr
        );
    }

    void render_loop() {
        AVPacket packet;
        av_init_packet(&packet);
        
        int frame_count = 0;
        int last_fid = -1;
        
        while (running) {
            Sleep(1);
            
            std::lock_guard<std::mutex> lock(frame_mutex);
            
            uint32_t data_size;
            memcpy(&data_size, shm_buffer, 4);
            
            if (data_size < 8 || data_size > SHM_SIZE) {
                continue;
            }
            
            uint32_t fid;
            uint32_t frame_size;
            memcpy(&fid, shm_buffer + 4, 4);
            memcpy(&frame_size, shm_buffer + 8, 4);
            
            if (fid == 0xFFFFFFFF) {
                uint32_t new_width, new_height;
                memcpy(&new_width, shm_buffer + 4, 4);
                memcpy(&new_height, shm_buffer + 8, 4);
                update_resolution(new_width, new_height);
                continue;
            }
            
            if (fid == last_fid) {
                continue;
            }
            
            last_fid = fid;
            
            if (frame_size > MAX_FRAME_SIZE) {
                std::cerr << "[C++渲染] 帧大小过大: " << frame_size << std::endl;
                continue;
            }
            
            packet.data = shm_buffer + 12;
            packet.size = frame_size;
            
            int ret = avcodec_send_packet(codec_ctx, &packet);
            if (ret < 0) {
                continue;
            }
            
            ret = avcodec_receive_frame(codec_ctx, frame);
            if (ret < 0) {
                continue;
            }
            
            if (!sws_ctx || codec_ctx->width != frame->width || codec_ctx->height != frame->height) {
                update_sws_context();
            }
            
            uint8_t* dest[4] = { rgb_buffer, nullptr, nullptr, nullptr };
            int dest_linesize[4] = { width * 4, 0, 0, 0 };
            
            sws_scale(sws_ctx, frame->data, frame->linesize, 0, frame->height, dest, dest_linesize);
            
            RECT rect;
            GetClientRect(target_window, &rect);
            StretchBlt(hdc_window, 0, 0, rect.right - rect.left, rect.bottom - rect.top,
                      hdc_mem, 0, 0, width, height, SRCCOPY);
            
            frame_count++;
            if (frame_count % 30 == 0) {
                std::cout << "[C++渲染] 已渲染 " << frame_count << " 帧" << std::endl;
            }
        }
    }

    void cleanup() {
        if (sws_ctx) {
            sws_freeContext(sws_ctx);
        }
        
        if (frame) {
            av_frame_free(&frame);
        }
        
        if (frame_rgb) {
            av_frame_free(&frame_rgb);
        }
        
        if (codec_ctx) {
            avcodec_free_context(&codec_ctx);
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
        
        std::cout << "[C++渲染] 清理完成" << std::endl;
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

    std::cout << "[C++渲染] 启动参数:" << std::endl;
    std::cout << "[C++渲染]   窗口句柄: " << hwnd << std::endl;
    std::cout << "[C++渲染]   共享内存: " << shm_name << std::endl;
    std::cout << "[C++渲染]   分辨率: " << width << "x" << height << std::endl;

    H264Renderer renderer(hwnd, shm_name, width, height);
    
    if (!renderer.init()) {
        std::cerr << "[C++渲染] 初始化失败" << std::endl;
        return 1;
    }

    renderer.start();

    std::cout << "[C++渲染] 按Enter键退出..." << std::endl;
    std::cin.get();

    renderer.stop();
    
    return 0;
}