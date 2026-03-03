import gc
import socket
import threading
import json
import random
import time
import struct
import tkinter as tk
import pynput.mouse as pm
import multiprocessing
import ctypes
import numpy as np
import av

# ================= 配置区域 =================
SERVER_IP = 'frp-hat.com'
SERVER_PORT = 43972
LISTEN_PORT = 9050

VIDEO_BITRATE = 3000
MAX_FPS = 60
MTU_SIZE = 1400
SHM_BUFFER_SIZE = 500 * 1024
# ===========================================

capture_process = None

FRAME_TYPE_HEADER = 0x4A4B4C4D
FRAME_TYPE_DATA   = 0x4A4B4C4E

def capture_process_task(shm_array, current_size, ready_flag, bitrate, max_fps, width=None, height=None):
    print("[子进程] 初始化中...")
    
    try:
        import dxcam
        camera = dxcam.create(output_color="BGR")
        camera.start(target_fps=max_fps, video_mode=True)
        frame = camera.get_latest_frame()
        if frame is None: return
        # 获取实际桌面分辨率
        actual_height, actual_width = frame.shape[:2]
        # 如果没有指定分辨率，使用实际分辨率
        if width is None or height is None:
            width, height = actual_width, actual_height
        print(f"[子进程] dxcam OK: {actual_width}x{actual_height} (目标: {width}x{height})")
    except Exception as e:
        print(f"[子进程错误] dxcam: {e}")
        return

    try:
        container = av.open('dummy.mpegts', 'w', format='mpegts')
        stream = container.add_stream('h264_mf')
        stream.width = width
        stream.height = height
        stream.pix_fmt = 'yuv420p'
        stream.bit_rate = bitrate * 1000
        stream.options = {
            'preset': 'ultrafast',
            'tune': 'zerolatency',
            'g': '15',
            'bf': '0',
            'rc-lookahead': '0',
        }
        print(f"[子进程] Encoder OK")
    except Exception as e:
        print(f"[子进程错误] Encoder: {e}")
        return

    last_stats = time.time()
    frame_count = 0
    
    while True:
        try:
            frame = camera.get_latest_frame()
            if frame is None: continue
            
            # === 生产者：忙等待 (最低延迟) ===
            # 只有当数据被取走 (flag=0) 才写入
            while ready_flag.value == 1:
                time.sleep(0)
            
            # 使用原始帧，不进行缩放，确保完整显示桌面
            # av_frame = av.VideoFrame.from_ndarray(frame, format='bgr24')
            av_frame = av.VideoFrame(width, height, 'bgr24')
            av_frame.planes[0].update(frame)

            packets = stream.encode(av_frame)
            
            for packet in packets:
                packet_data = bytes(packet)
                data_len = len(packet_data)
                
                if data_len < SHM_BUFFER_SIZE:
                    shm_array[0:4] = struct.pack('I', data_len)
                    shm_array[4 : 4+data_len] = packet_data
                    current_size.value = data_len
                    ready_flag.value = 1
            
            frame_count += 1
            if time.time() - last_stats >= 1.0:
                #print(f"[子进程] FPS: {frame_count}")
                frame_count = 0
                last_stats = time.time()
            
        except Exception as e:
            print(f"[子进程循环错误] {e}")

class P2PControlledApp:
    def __init__(self):
        #禁用gc
        import gc
        gc.disable()

        self.root = tk.Tk()
        self.root.title("远程桌面-极速版")
        self.root.geometry("400x250")
        
        self.code = self.generate_code()
        self.running = True
        self.is_connected = False
        self.controller_addr = None
        self.last_heartbeat = 0
        self.frame_id = 0
        self.mouse = pm.Controller()
        
        self.udp_socket = None
        self.shm_array = multiprocessing.RawArray(ctypes.c_ubyte, SHM_BUFFER_SIZE)
        self.shared_size = multiprocessing.RawValue(ctypes.c_int, 0)
        self.ready_flag = multiprocessing.RawValue(ctypes.c_int, 0)
        
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
        self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8*1024*1024) 
        self.udp_socket.bind(('0.0.0.0', LISTEN_PORT))
        threading.Thread(target=self.udp_listener_loop, daemon=True).start()
        
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

    def udp_listener_loop(self):
        self.udp_socket.settimeout(1.0)
        while self.running:
            try:
                data, addr = self.udp_socket.recvfrom(1024)
                try:
                    msg = json.loads(data.decode())
                    if msg.get('type') == 'punch': self.reset_connection(addr)
                except: pass
                
                if self.is_connected and addr == self.controller_addr:
                    self.last_heartbeat = time.time()
                    try:
                        cmd = json.loads(data.decode())
                        if cmd['type'] == 'mouse':
                            self.mouse.position = (cmd['x'], cmd['y'])
                            if cmd.get('click'): self.mouse.click(pm.Button.left, 1)
                        elif cmd['type'] == 'resolution':
                                # 接收分辨率设置
                                new_width = cmd['width']
                                new_height = cmd['height']
                                print(f"[设置] 分辨率: {new_width}x{new_height}")
                                # 重启捕获进程以应用新分辨率
                                global capture_process
                                if capture_process and capture_process.is_alive():
                                    capture_process.terminate()
                                self.shared_size.value = 0
                                self.ready_flag.value = 0
                                capture_process = multiprocessing.Process(
                                    target=capture_process_task, 
                                    args=(self.shm_array, self.shared_size, self.ready_flag, VIDEO_BITRATE, MAX_FPS, new_width, new_height),
                                    daemon=True
                                )
                                capture_process.start()
                                print(f"[子进程] 重启以应用新分辨率: {new_width}x{new_height}")
                    except: pass
            except socket.timeout:
                if self.is_connected and time.time() - self.last_heartbeat > 5.0:
                    self.handle_disconnect("心跳超时")
            except: pass

    def reset_connection(self, addr):
        if self.is_connected and self.controller_addr == addr: return
        print(f"[状态] 连接: {addr}")
        self.controller_addr = addr
        self.is_connected = True
        self.last_heartbeat = time.time()
        self.udp_socket.sendto(b"PUNCH_OK", self.controller_addr)
        self.update_status(f"已连接: {addr}")
        
        global capture_process
        if capture_process and capture_process.is_alive(): capture_process.terminate()
        self.shared_size.value = 0
        self.ready_flag.value = 0
        
        capture_process = multiprocessing.Process(
            target=capture_process_task, 
            args=(self.shm_array, self.shared_size, self.ready_flag, VIDEO_BITRATE, MAX_FPS),
            daemon=True
        )
        capture_process.start()

        threading.Thread(target=self.stream_sender, daemon=True).start()

    def stream_sender(self):
        mv = memoryview(self.shm_array)
        frame_count = 0; last_stats = time.time()
        
        send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # === 核心：非阻塞模式 ===
        send_socket.setblocking(False)
        send_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8*1024*1024)
        bind_ip = self.controller_addr[0]; send_socket.bind((bind_ip, 0)) 
        
        while self.is_connected and self.running:
            try:
                # === 消费者：忙等待 ===
                if self.ready_flag.value == 1:
                    actual_len = struct.unpack('I', mv[0:4])[0]
                    packet_data = mv[4 : 4+actual_len]
                    self.ready_flag.value = 0
                    
                    self.send_packet(send_socket, packet_data)
                    
                    frame_count += 1
                    if time.time() - last_stats >= 1.0:
                        self.update_stats(frame_count)
                        frame_count = 0; last_stats = time.time()
                else:
                    time.sleep(0)
                    
            except Exception as e:
                print(f"[发送错误] {e}")
            
    def send_packet(self, sock, packet_data):
        if not self.controller_addr: return
        try:
            self.frame_id = (self.frame_id + 1) % 99999999
            fid = self.frame_id
            total_size = len(packet_data)
            
            header = struct.pack('!III', FRAME_TYPE_HEADER, fid, total_size)
            sock.sendto(header, self.controller_addr)
            
            # for i in range(0, total_size, MTU_SIZE):
            #     chunk = packet_data[i : i + MTU_SIZE]
            #     chunk_header = struct.pack('!IIIH', FRAME_TYPE_DATA, fid, total_size, i // MTU_SIZE)
            #     sock.sendto(chunk_header + chunk, self.controller_addr)
            i = 0
            while i < total_size:
                chunk = packet_data[i:i+MTU_SIZE]
                chunk_header = struct.pack('!IIIH', FRAME_TYPE_DATA, fid, total_size, i // MTU_SIZE)
                sock.sendto(chunk_header + chunk, self.controller_addr)
                i += MTU_SIZE    
        except BlockingIOError:
            pass # 缓冲区满，直接丢弃该分片 (网络拥塞时的最佳策略)
        except: pass

    def handle_disconnect(self, reason):
        self.is_connected = False; self.update_status(f"状态: {reason}")
        global capture_process
        if capture_process and capture_process.is_alive(): capture_process.terminate()

    def update_status(self, text): 
        if self.running: self.status_label.config(text=text)
    def update_stats(self, fps): 
        if self.running: self.stats_label.config(text=f"FPS: {fps}")

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.mainloop()
    def on_close(self):
        self.running = False
        global capture_process
        if capture_process and capture_process.is_alive(): capture_process.terminate()
        self.root.destroy()

if __name__ == "__main__":
    multiprocessing.freeze_support()
    app = P2PControlledApp()
    app.run()