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
from tkinter import font as tkfont

# ================= 配置区域 =================
TCP_LISTEN_PORT = 5667
VIDEO_BITRATE = 4000
MAX_FPS = 90
MTU_SIZE = 1400
SHM_BUFFER_SIZE = 2 * 1024 * 1024
CRF_VALUE = 23
MIN_BITRATE = 4000
MAX_BITRATE = 6000
# ===========================================

capture_process = None

FRAME_TYPE_HEADER = 0x4A4B4C4D
FRAME_TYPE_DATA   = 0x4A4B4C4E

def capture_process_task(shm_array, current_size, ready_event, bitrate, max_fps, width=None, height=None, network_quality=None):
    print("[子进程] 初始化中...")
    
    try:
        import dxcam
        camera = dxcam.create(
            output_color="BGRA",
            max_buffer_len=1
        )
        camera.start(target_fps=max_fps, video_mode=True)
        frame = camera.get_latest_frame()
        if frame is None: return
        actual_height, actual_width = frame.shape[:2]
        if width is None or height is None:
            width, height = actual_width, actual_height
        print(f"[子进程] dxcam OK: {actual_width}x{actual_height} (目标: {width}x{height})")
    except Exception as e:
        print(f"[子进程错误] dxcam: {e}")
        return

    try:
        container = av.open('dummy.mpegts', 'w', format='mpegts')
        stream = container.add_stream('libx264')
        stream.width = width
        stream.height = height
        stream.pix_fmt = 'yuv420p'
        stream.options = {
            'preset': 'ultrafast',
            'tune': 'zerolatency',
            'crf': '24',
            'bf': '0',
            'refs': '1',
            'rc-lookahead': '0',
            'g': '30',
            'keyint_min': '30',
            'scenecut': '0',
            'aq-mode': '1',
            'aq-strength': '1.0'
        }
        print(f"[子进程] Encoder OK - CRF:24")
    except Exception as e:
        print(f"[子进程错误] Encoder: {e}")
        return

    last_stats = time.time()
    frame_count = 0
    
    while True:
        try:
            frame = camera.get_latest_frame()
            if frame is None: 
                continue
            
            if ready_event.is_set():
                time.sleep(0.001)
                continue
            
            av_frame = av.VideoFrame.from_ndarray(frame, format='bgra')
            
            if av_frame.width != width or av_frame.height != height:
                av_frame = av_frame.reformat(width=width, height=height)

            packets = stream.encode(av_frame)
            
            for packet in packets:
                packet_data = bytes(packet)
                data_len = len(packet_data)
                
                if data_len < SHM_BUFFER_SIZE:
                    shm_array[0:4] = struct.pack('I', data_len)
                    shm_array[4 : 4+data_len] = packet_data
                    current_size.value = data_len
                    ready_event.set()
            
            frame_count += 1
            
            if time.time() - last_stats >= 1.0:
                actual_fps = frame_count
                frame_count = 0
                last_stats = time.time()
                
                if network_quality is not None:
                    network_quality.value = actual_fps
                
                #print(f"[子进程] 实际FPS: {actual_fps}")
            
            
        except Exception as e:
            print(f"[子进程循环错误] {e}")

class P2PControlledApp:
    def __init__(self):
        gc.disable()

        self.root = tk.Tk()
        self.root.title("远程桌面控制")
        
        # === 修改部分开始 ===
        # 移除 overrideredirect(True)，恢复系统标题栏和任务栏显示
        # self.root.overrideredirect(True) 
        
        # === ToDesk 风格配置 ===
        self.width = 360
        self.height = 400
        self.bg_color = "#1F1F2E"       # 深蓝灰背景
        self.card_bg = "#2A2A3C"        # 卡片背景
        self.accent_color = "#0078D4"   # 强调色（蓝）
        self.text_white = "#FFFFFF"
        self.text_gray = "#A0A0B0"
        
        # 居中窗口
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = (screen_w - self.width) // 2
        y = (screen_h - self.height) // 2
        self.root.geometry(f"{self.width}x{self.height}+{x}+{y}")
        self.root.configure(bg=self.bg_color)
        
        # 设置窗口图标（可选，如果有ico文件）
        # self.root.iconbitmap("icon.ico")

        self.code = self.generate_code()
        self.running = True
        self.is_connected = False
        self.controller_addr = None
        self.last_heartbeat = 0
        self.frame_id = 0
        self.mouse = pm.Controller()
        
        # === 网络初始化 ===
        self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8*1024*1024)
        self.udp_socket.bind(('0.0.0.0', 0))
        
        self.local_ip = self.get_local_ip()
        self.local_udp_port = self.udp_socket.getsockname()[1]
        
        # 共享内存
        self.shm_array = multiprocessing.RawArray(ctypes.c_ubyte, SHM_BUFFER_SIZE)
        self.shared_size = multiprocessing.RawValue(ctypes.c_int, 0)
        self.ready_event = multiprocessing.Event()
        self.network_quality = multiprocessing.RawValue(ctypes.c_int, 0)
        
        # === UI 构建 ===
        self.setup_ui()
        
        # 启动 TCP 信令服务
        threading.Thread(target=self.tcp_signaling_server, daemon=True).start()
        
        # 启动 UDP 监听
        threading.Thread(target=self.udp_listener_loop, daemon=True).start()

    def setup_ui(self):
        # 1. 顶部工具栏 (放置最小化和关闭按钮)
        # 由于恢复了系统标题栏，我们将按钮放在内容区顶部，或者尝试自定义标题栏按钮的位置
        # 这里我们将按钮放在内容区的右上角，保持 UI 整洁
        
        top_bar = tk.Frame(self.root, bg=self.bg_color)
        top_bar.pack(fill=tk.X, side=tk.TOP)
        
        # # 右侧按钮组
        # btn_frame = tk.Frame(top_bar, bg=self.bg_color)
        # btn_frame.pack(side=tk.RIGHT, padx=5, pady=5)

        # # 最小化按钮
        # min_btn = tk.Label(btn_frame, text=" — ", bg=self.bg_color, fg=self.text_gray, 
        #                      font=("Arial", 10, "bold"), cursor="hand2")
        # min_btn.pack(side=tk.LEFT, padx=2)
        # min_btn.bind("<Button-1>", lambda e: self.root.iconify()) # 最小化
        # min_btn.bind("<Enter>", lambda e: min_btn.config(bg="#3D3D50"))
        # min_btn.bind("<Leave>", lambda e: min_btn.config(bg=self.bg_color))

        # # 关闭按钮
        # close_btn = tk.Label(btn_frame, text=" × ", bg=self.bg_color, fg=self.text_gray, 
        #                      font=("Arial", 12, "bold"), cursor="hand2")
        # close_btn.pack(side=tk.LEFT, padx=2)
        # close_btn.bind("<Button-1>", lambda e: self.on_close())
        # close_btn.bind("<Enter>", lambda e: close_btn.config(fg="red", bg="#3D3D50"))
        # close_btn.bind("<Leave>", lambda e: close_btn.config(fg=self.text_gray, bg=self.bg_color))

        # 2. 设备代码卡片
        card_frame = tk.Frame(self.root, bg=self.card_bg)
        card_frame.pack(fill=tk.X, padx=15, pady=10, ipady=15)
        
        # 设备名称
        tk.Label(card_frame, text="远端连接口令", bg=self.card_bg, fg=self.text_gray,
                 font=("Microsoft YaHei", 9)).pack(pady=(10, 0))
        
        # 连接口令 (大号字体，蓝色高亮)
        self.code_label = tk.Label(card_frame, text=self.code, bg=self.card_bg, 
                                   fg=self.accent_color, font=("Consolas", 48, "bold"))
        self.code_label.pack(pady=10)
        
        # 分隔线
        sep = tk.Frame(card_frame, height=1, bg="#3D3D50")
        sep.pack(fill=tk.X, padx=20, pady=10)
        
        # 状态显示
        self.status_label = tk.Label(card_frame, text="等待连接...", bg=self.card_bg, 
                                     fg="#4CAF50", font=("Microsoft YaHei", 10))
        self.status_label.pack(pady=(0, 10))
        
        # 3. 信息区域
        info_frame = tk.Frame(self.root, bg=self.bg_color)
        info_frame.pack(fill=tk.X, padx=15, pady=10)
        
        self.create_info_row(info_frame, "本机IP:", self.local_ip)
        self.create_info_row(info_frame, "TCP端口:", str(TCP_LISTEN_PORT))
        
        # 4. 底部状态栏
        bottom_bar = tk.Frame(self.root, bg=self.bg_color)
        bottom_bar.pack(side=tk.BOTTOM, fill=tk.X, pady=10)
        
        self.stats_label = tk.Label(bottom_bar, text="P2P Mode Active", bg=self.bg_color, 
                                    fg=self.text_gray, font=("Arial", 8))
        self.stats_label.pack()

    def create_info_row(self, parent, label, value):
        f = tk.Frame(parent, bg=self.bg_color)
        f.pack(fill=tk.X, pady=3)
        tk.Label(f, text=label, bg=self.bg_color, fg=self.text_gray, 
                 font=("Microsoft YaHei", 9), width=8, anchor='w').pack(side=tk.LEFT)
        tk.Label(f, text=value, bg=self.bg_color, fg=self.text_white, 
                 font=("Consolas", 9), anchor='w').pack(side=tk.LEFT)

    # === 移除窗口拖动逻辑 (系统标题栏已接管) ===
    # def start_move...
    # def do_move...

    def get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"

    def generate_code(self): 
        return str(random.randint(100000, 999999))

    def tcp_signaling_server(self):
        """内置 TCP 信令服务"""
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server_sock.bind(('0.0.0.0', TCP_LISTEN_PORT))
            server_sock.listen(5)
            print(f"[信令] TCP 监听端口: {TCP_LISTEN_PORT}")
        except Exception as e:
            self.update_status(f"TCP 端口 {TCP_LISTEN_PORT} 绑定失败")
            print(f"[信令错误] {e}")
            return

        while self.running:
            try:
                server_sock.settimeout(1.0)
                conn, addr = server_sock.accept()
                threading.Thread(target=self.handle_tcp_client, args=(conn, addr), daemon=True).start()
            except socket.timeout:
                continue
            except Exception:
                pass

    def handle_tcp_client(self, conn, addr):
        """处理 TCP 握手"""
        try:
            conn.settimeout(5.0)
            data = conn.recv(1024).decode()
            msg = json.loads(data)

            # 验证口令
            if msg.get('type') == 'connect' and msg.get('code') == self.code:
                client_public_ip = addr[0]
                client_public_port = msg['udp_port']
                
                client_local_ip = msg.get('local_ip')
                client_local_port = msg.get('local_port')

                print(f"[信令] 验证通过: {client_public_ip}:{client_public_port}")

                resp = {
                    'status': 'ok',
                    'peer_public_port': self.local_udp_port,
                    'peer_local_ip': self.local_ip,
                    'peer_local_port': self.local_udp_port
                }
                conn.send(json.dumps(resp).encode())
                
                self.controller_addr = (client_public_ip, client_public_port)
                self.is_connected = True
                self.last_heartbeat = time.time()
                
                threading.Thread(target=self.punch_worker, args=((client_public_ip, client_public_port),), daemon=True).start()
                
                if client_local_ip and client_local_port:
                    threading.Thread(target=self.punch_worker, args=((client_local_ip, client_local_port),), daemon=True).start()

                self.update_status(f"已连接: {client_public_ip}")
                
                threading.Thread(target=self.stream_sender, daemon=True).start()
                
                global capture_process
                if capture_process and capture_process.is_alive(): 
                    capture_process.terminate()
                self.shared_size.value = 0
                self.ready_event.clear()
                self.network_quality.value = 0
                capture_process = multiprocessing.Process(
                    target=capture_process_task, 
                    args=(self.shm_array, self.shared_size, self.ready_event, VIDEO_BITRATE, MAX_FPS, None, None, self.network_quality),
                    daemon=True
                )
                capture_process.start()
            else:
                conn.send(json.dumps({'status': 'invalid_code'}).encode())
        except Exception as e:
            print(f"[信令处理错误] {e}")
        finally:
            conn.close()

    def punch_worker(self, target_addr):
        punch_msg = b"PUNCH_OK"
        count = 0
        while self.running and self.is_connected and count < 100:
            try:
                self.udp_socket.sendto(punch_msg, target_addr)
                time.sleep(0.05) 
                count += 1
            except:
                break

    def udp_listener_loop(self):
        self.udp_socket.settimeout(1.0)
        while self.running:
            try:
                data, addr = self.udp_socket.recvfrom(1024)
                if not self.is_connected: continue
                
                if data == b"HEARTBEAT":
                    self.last_heartbeat = time.time()
                    continue

                try:
                    cmd = json.loads(data.decode())
                    if cmd['type'] == 'mouse':
                        self.mouse.position = (cmd['x'], cmd['y'])
                        if cmd.get('click'): 
                            btn = pm.Button.left if cmd.get('button') == 'left' else pm.Button.right
                            self.mouse.click(btn, 1)
                    elif cmd['type'] == 'key':
                        pass
                    elif cmd['type'] == 'resolution':
                        global capture_process
                        if capture_process and capture_process.is_alive(): capture_process.terminate()
                        self.shared_size.value = 0
                        self.ready_event.clear()
                        self.network_quality.value = 0
                        capture_process = multiprocessing.Process(
                            target=capture_process_task, 
                            args=(self.shm_array, self.shared_size, self.ready_event, VIDEO_BITRATE, MAX_FPS, cmd['width'], cmd['height'], self.network_quality),
                            daemon=True
                        )
                        capture_process.start()
                except: pass

            except socket.timeout:
                if self.is_connected and time.time() - self.last_heartbeat > 5.0:
                    self.handle_disconnect("连接超时")
            except: pass

    def stream_sender(self):
        mv = memoryview(self.shm_array)
        frame_count = 0; last_stats = time.time()
        
        send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send_socket.setblocking(False)
        send_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8*1024*1024)
        
        while self.is_connected and self.running:
            try:
                if self.ready_event.is_set():
                    actual_len = struct.unpack('I', mv[0:4])[0]
                    packet_data = mv[4 : 4+actual_len]
                    self.ready_event.clear()
                    
                    self.send_packet(send_socket, packet_data)
                    
                    frame_count += 1
                    if time.time() - last_stats >= 1.0:
                        self.update_stats(frame_count)
                        frame_count = 0; last_stats = time.time()
                else:
                    time.sleep(0.001)
            except Exception as e:
                print(f"[发送错误] {e}")
            
    def send_packet(self, sock, packet_data):
        if not self.controller_addr: return
        try:
            self.frame_id = (self.frame_id + 1) & 0xFFFF
            fid = self.frame_id
            total_size = len(packet_data)
            
            header = struct.pack('!III', FRAME_TYPE_HEADER, fid, total_size)
            sock.sendto(header, self.controller_addr)
            
            i = 0
            mv = memoryview(packet_data)
            while i < total_size:
                chunk = mv[i:i+MTU_SIZE]
                chunk_header = struct.pack('!IIIH', FRAME_TYPE_DATA, fid, total_size, i // MTU_SIZE)
                sock.sendto(chunk_header + chunk, self.controller_addr)
                i += MTU_SIZE    
        except BlockingIOError:
            pass 
        except: pass

    def handle_disconnect(self, reason):
        self.is_connected = False
        self.update_status(f"断开: {reason}")
        global capture_process
        if capture_process and capture_process.is_alive(): capture_process.terminate()

    # === UI 更新函数 ===
    def update_status(self, text): 
        if self.running: 
            self.status_label.config(text=text, fg="#4CAF50" if "已连接" in text else "#FF9800")
            
    def update_stats(self, fps): 
        if self.running: 
            self.stats_label.config(text=f"传输速率: {fps} FPS")

    def run(self):
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