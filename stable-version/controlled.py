import socket
import threading
import json
import random
import time
import struct
import tkinter as tk
from queue import Queue
import mss
import pynput.mouse as pm
from turbojpeg import TurboJPEG
import numpy as np
import cv2
import multiprocessing
import os

# ================= 配置区域 =================
SERVER_IP = 'frp-hat.com' # 修改这里
SERVER_PORT = 43972
LISTEN_PORT = 9050

# 性能配置
TARGET_WIDTH = 1280
JPEG_QUALITY = 75
MTU_SIZE = 1300

# 【重要】TurboJPEG DLL 路径配置
# 如果报错 "Unable to locate turbojpeg library"，请取消下面一行的注释并修改为你的实际路径
# 例如: r"C:\Users\EDY\turbojpeg\turbojpeg.dll"
TURBOJPEG_DLL_PATH = 'C:\\libjpeg-turbo-gcc64\\bin\\libturbojpeg.dll'  # 默认自动寻找
# ===========================================

# 进程间通信队列
frame_queue = multiprocessing.Queue(maxsize=1)

def capture_process_task(queue, target_width, quality, lib_path):
    """
    独立进程：负责高强度的截图、缩放、编码
    """
    # 1. 在子进程内部初始化 TurboJPEG
    try:
        jpeg = TurboJPEG(lib_path=lib_path)
    except Exception as e:
        print(f"[子进程错误] TurboJPEG 初始化失败: {e}")
        print("[提示] 请在代码顶部的 TURBOJPEG_DLL_PATH 变量中手动指定 DLL 的绝对路径。")
        return

    print("[子进程] 截图与编码进程已启动")
    
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        
        while True:
            try:
                # 1. 截图 (mss 返回的是 BGRA 数据)
                img = sct.grab(monitor)
                
                # 转换为 numpy 数组
                # mss 的最新版推荐直接转为 np.array
                frame = np.array(img)
                
                # 2. OpenCV 缩放 (比 Pillow 快得多)
                h, w = frame.shape[:2]
                if w > target_width:
                    ratio = target_width / w
                    new_h = int(h * ratio)
                    # INTER_LINEAR 速度快
                    frame = cv2.resize(frame, (target_width, new_h), interpolation=cv2.INTER_LINEAR)
                
                # 3. TurboJPEG 编码
                # OpenCV 默认是 BGRA，TurboJPEG 支持，或者转 BGR
                # 为保险起见，转为 BGR (OpenCV 标准格式)
                frame_bgr = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                
                # 编码
                frame_data = jpeg.encode(frame_bgr, quality=quality)
                
                # 4. 放入队列
                if queue.full():
                    try: 
                        queue.get_nowait() # 丢弃旧帧
                    except: pass
                queue.put(frame_data)
                
            except Exception as e:
                print(f"Capture Proc Error: {e}")
                time.sleep(0.1)

class P2PControlledApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("远程桌面-P2P被控端")
        self.root.geometry("400x250")
        
        # 主进程仅用于UI和状态显示，不需要初始化 jpeg
        self.code = self.generate_code()
        self.running = True
        self.is_connected = False
        self.controller_addr = None
        self.last_heartbeat = 0
        self.frame_id = 0
        
        self.udp_socket = None
        self.send_queue = Queue(maxsize=1)
        
        self.capture_process = None
        
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
        self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024*1024)
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
                if not self.is_connected:
                    try:
                        msg = json.loads(data.decode())
                        if msg.get('type') == 'punch':
                            self.controller_addr = addr
                            self.is_connected = True
                            self.last_heartbeat = time.time()
                            self.udp_socket.sendto(b"PUNCH_OK", self.controller_addr)
                            self.update_status(f"P2P直连: {addr[0]}")
                            
                            # 启动独立采集进程，传入 DLL 路径
                            if self.capture_process is None:
                                self.capture_process = multiprocessing.Process(
                                    target=capture_process_task, 
                                    args=(frame_queue, TARGET_WIDTH, JPEG_QUALITY, TURBOJPEG_DLL_PATH),
                                    daemon=True
                                )
                                self.capture_process.start()
                            
                            # 启动发送线程
                            threading.Thread(target=self.stream_sender, daemon=True).start()
                            threading.Thread(target=self.udp_sender_thread, daemon=True).start()
                    except: pass
                else:
                    if addr == self.controller_addr:
                        self.last_heartbeat = time.time()
                        if data != b"HEARTBEAT": self.handle_command(data)
            except: pass

    def udp_sender_thread(self):
        while self.is_connected and self.running:
            try:
                frame_data = self.send_queue.get(timeout=1.0)
                self.send_frame(frame_data)
            except: pass

    def send_frame(self, frame_data):
        if not self.controller_addr: return
        try:
            self.frame_id = (self.frame_id + 1) % 99999999
            fid = self.frame_id
            total_size = len(frame_data)
            
            header = struct.pack('!10sII', b'FRAMESTAR', fid, total_size)
            for _ in range(3):
                self.udp_socket.sendto(header, self.controller_addr)
            
            chunks = [frame_data[i:i+MTU_SIZE] for i in range(0, total_size, MTU_SIZE)]
            for i, chunk in enumerate(chunks):
                chunk_header = struct.pack('!10sIIH', b'FRAMEDATA', fid, total_size, i)
                self.udp_socket.sendto(chunk_header + chunk, self.controller_addr)
        except: pass

    def stream_sender(self):
        frame_count = 0
        last_stats = time.time()
        
        while self.is_connected and self.running:
            if time.time() - self.last_heartbeat > 15.0:
                self.handle_disconnect("超时"); break
            
            try:
                frame_data = frame_queue.get(timeout=1.0)
                
                if self.send_queue.full():
                    try: self.send_queue.get_nowait()
                    except: pass
                self.send_queue.put(frame_data)
                
                frame_count += 1
                if time.time() - last_stats >= 1.0:
                    self.update_stats(frame_count)
                    frame_count = 0
                    last_stats = time.time()
                    
            except: pass
        self.handle_disconnect("结束")
        
    def handle_command(self, data):
        try:
            cmd = json.loads(data.decode())
            if cmd['type'] == 'mouse':
                pm.Controller().position = (cmd['x'], cmd['y'])
                if cmd.get('click'): pm.Controller().click(pm.Button.left, 1)
        except: pass

    def handle_disconnect(self, reason):
        self.is_connected = False; self.controller_addr = None
        self.update_status(f"状态: {reason}")

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