import socket
import threading
import json
import random
import time
import struct
import tkinter as tk
import pynput.mouse as pm
from turbojpeg import TurboJPEG
import numpy as np
import multiprocessing
import ctypes

# ================= 配置区域 =================
SERVER_IP = 'frp-hat.com' # 修改这里
SERVER_PORT = 43972
LISTEN_PORT = 9050

JPEG_QUALITY = 100        # 原画画质
# 注意：部分运营商可能会丢弃过大的 UDP 包。如果花屏，请改回 1400
MTU_SIZE = 5600         

# TurboJPEG DLL 路径 (必填，子进程无法自动寻找)
TURBOJPEG_DLL_PATH = 'C:\\libjpeg-turbo-gcc64\\bin\\libturbojpeg.dll'  # 例如: C:\\libjpeg-turbo-gcc64\\bin\\libturbojpeg.dll        
# 共享内存大小 (2MB 足够放下 1080P 原画 JPEG)
SHM_BUFFER_SIZE = 3 * 1024 * 1024 
# ===========================================

def capture_process_task(shm_buffer, current_size, ready_flag, quality, lib_path):
    """
    截图进程：持续截图并更新到共享内存
    """
    try:
        import dxcam
        jpeg = TurboJPEG(lib_path=lib_path)
    except Exception as e:
        print(f"[子进程错误] 初始化失败: {e}")
        return

    print("[子进程] 共享内存模式已启动")
    camera = dxcam.create(output_color="BGR")
    
    while True:
        try:
            frame = camera.grab()
            if frame is None: continue
            
            # 编码
            frame_data = jpeg.encode(frame, quality=quality)
            data_len = len(frame_data)
            
            # === 修正逻辑 ===
            # 1. 检查大小是否超出共享内存上限 (使用局部变量 SHM_BUFFER_SIZE)
            if data_len < SHM_BUFFER_SIZE:
                # 2. 写入数据内容 (直接写入共享内存)
                shm_buffer[:data_len] = frame_data
                
                # 3. 更新当前大小 (Value)
                current_size.value = data_len
                
                # 4. 设置标志位为就绪 (1)
                ready_flag.value = 1
            else:
                # 这种情况极少发生，除非画质设到 100 且画面极度复杂
                print(f"[警告] 图片过大 ({data_len/1024:.0f}KB)，超过共享内存限制")
                
        except Exception as e:
            print(f"Capture Error: {e}")
            time.sleep(0.1)

class P2PControlledApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("远程桌面-共享内存版")
        self.root.geometry("400x250")
        
        self.code = self.generate_code()
        self.running = True
        self.is_connected = False
        self.controller_addr = None
        self.last_heartbeat = 0
        self.frame_id = 0
        
        self.udp_socket = None
        self.capture_process = None
        
        # === 共享内存初始化 ===
        # RawArray: 原始字节数组，用于存图片数据
        self.shm_buffer = multiprocessing.RawArray(ctypes.c_ubyte, SHM_BUFFER_SIZE)
        # Value: 整数，用于记录当前图片的实际大小
        self.current_size = multiprocessing.Value(ctypes.c_int, 0)
        # Value: 标志位 (0:已读/空闲, 1:新数据就绪)
        self.ready_flag = multiprocessing.Value(ctypes.c_int, 0)
        
        # GUI
        tk.Label(self.root, text="口令:").pack(pady=5)
        self.code_label = tk.Label(self.root, text=self.code, font=("Arial", 32, "bold"), fg="blue")
        self.code_label.pack()
        self.status_label = tk.Label(self.root, text="状态: 正在注册...", fg="gray")
        self.status_label.pack(pady=5)
        self.stats_label = tk.Label(self.root, text="等待连接")
        self.stats_label.pack()
        
        threading.Thread(target=self.start_p2p_service, daemon=True).start()

    def generate_code(self): return str(random.randint(100000, 999999))

    def start_p2p_service(self):
        self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4*1024*1024) 
        self.udp_socket.bind(('0.0.0.0', LISTEN_PORT))
        threading.Thread(target=self.udp_listener, daemon=True).start()
        
        try:
            tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tcp_sock.connect((SERVER_IP, SERVER_PORT))
            reg_msg = {'type': 'register', 'code': self.code, 'udp_port': LISTEN_PORT}
            tcp_sock.send(json.dumps(reg_msg).encode())
            self.update_status("已注册，等待连接...")
            while self.running:
                try:
                    tcp_sock.settimeout(5.0)
                    if not tcp_sock.recv(1024): break
                except: continue
        except Exception as e:
            self.update_status(f"注册失败: {e}")

    def udp_listener(self):
        self.udp_socket.settimeout(1.0)
        while self.running:
            try:
                data, addr = self.udp_socket.recvfrom(65535)
                
                try:
                    msg = json.loads(data.decode())
                    if msg.get('type') == 'punch':
                        self.reset_connection(addr)
                except: pass
                
                if self.is_connected and addr == self.controller_addr:
                    self.last_heartbeat = time.time()
                    if data != b"HEARTBEAT": self.handle_command(data)
                    
            except socket.timeout:
                if self.is_connected and time.time() - self.last_heartbeat > 5.0:
                    self.handle_disconnect("心跳超时")
            except: pass

    def reset_connection(self, addr):
        print(f"[状态] 接受连接: {addr}")
        self.controller_addr = addr
        self.is_connected = True
        self.last_heartbeat = time.time()
        self.frame_id = 0
        
        self.udp_socket.sendto(b"PUNCH_OK", self.controller_addr)
        self.update_status(f"P2P直连: {addr[0]}")
        
        if self.capture_process and self.capture_process.is_alive():
            self.capture_process.terminate()
        
        self.ready_flag.value = 0
        
        self.capture_process = multiprocessing.Process(
            target=capture_process_task, 
            args=(self.shm_buffer, self.current_size, self.ready_flag, JPEG_QUALITY, TURBOJPEG_DLL_PATH),
            daemon=True
        )
        self.capture_process.start()
        
        threading.Thread(target=self.stream_sender, daemon=True).start()

    def stream_sender(self):
        frame_count = 0
        last_stats = time.time()
        
        while self.is_connected and self.running:
            try:
                # 1. 检查标志位
                if self.ready_flag.value == 1:
                    # 2. 读取数据长度
                    data_len = self.current_size.value
                    
                    # 3. 从共享内存读取数据 (必须 copy，否则发送时内存可能变化)
                    frame_data = bytes(self.shm_buffer[:data_len])
                    
                    # 4. 重置标志位 (告诉截图进程：可以写新数据了)
                    self.ready_flag.value = 0
                    
                    # 5. 发送
                    self.send_frame(frame_data)
                    
                    frame_count += 1
                    if time.time() - last_stats >= 1.0:
                        self.update_stats(frame_count)
                        frame_count = 0
                        last_stats = time.time()
                else:
                    # 没有新数据，稍微休眠
                    time.sleep(0.001)
                    
            except Exception as e:
                print(f"Sender Error: {e}")
                break
                
        self.handle_disconnect("发送结束")
        
    def send_frame(self, frame_data):
        if not self.controller_addr: return
        try:
            self.frame_id = (self.frame_id + 1) % 99999999
            fid = self.frame_id
            total_size = len(frame_data)
            
            header = struct.pack('!10sII', b'FRAMESTAR', fid, total_size)
            self.udp_socket.sendto(header, self.controller_addr)
            
            chunks = [frame_data[i:i+MTU_SIZE] for i in range(0, total_size, MTU_SIZE)]
            for i, chunk in enumerate(chunks):
                chunk_header = struct.pack('!10sIIH', b'FRAMEDATA', fid, total_size, i)
                self.udp_socket.sendto(chunk_header + chunk, self.controller_addr)
                
        except: pass

    def handle_command(self, data):
        try:
            cmd = json.loads(data.decode())
            if cmd['type'] == 'mouse':
                pm.Controller().position = (cmd['x'], cmd['y'])
                if cmd.get('click'): pm.Controller().click(pm.Button.left, 1)
        except: pass

    def handle_disconnect(self, reason):
        if not self.is_connected: return
        self.is_connected = False
        self.controller_addr = None
        self.update_status(f"状态: {reason}")
        if self.capture_process and self.capture_process.is_alive():
            self.capture_process.terminate()

    def update_status(self, text):
        if self.running: self.status_label.config(text=text)
    def update_stats(self, fps):
        if self.running: self.stats_label.config(text=f"FPS: {fps}")

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.mainloop()
    def on_close(self):
        self.running = False
        if self.capture_process and self.capture_process.is_alive():
            self.capture_process.terminate()
        self.root.destroy()

if __name__ == "__main__":
    multiprocessing.freeze_support()
    app = P2PControlledApp()
    app.run()