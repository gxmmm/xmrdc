import socket
import threading
import json
import time
import tkinter as tk
from PIL import Image, ImageTk
import struct
import av
import io
import numpy as np
import threading as _threading
import platform
import gc
import os

# Windows特定优化
if platform.system() == 'Windows':
    try:
        import ctypes
        import psutil
        # 设置进程优先级为高
        kernel32 = ctypes.windll.kernel32
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), 0x00000003)  # THREAD_PRIORITY_HIGH
        print("[优化] Windows线程优先级已设置为高")
        
        # 设置进程工作集大小，优化内存使用
        kernel32.SetProcessWorkingSetSize(kernel32.GetCurrentProcess(), -1, -1)
        print("[优化] Windows内存管理已优化")
        
        # 设置线程亲和性，提高缓存命中率
        p = psutil.Process(os.getpid())
        cpu_count = os.cpu_count()
        if cpu_count > 1:
            p.cpu_affinity([0, 1])
            print(f"[优化] 进程已绑定到核心 0, 1")
    except Exception as e:
        print(f"[优化] Windows优化失败: {e}")

SERVER_IP = 'frp-hat.com'
SERVER_PORT = 43972
MTU_SIZE = 1400  # 标准以太网 MTU
LOCAL_UDP_PORT = 20000 

FRAME_TYPE_HEADER = 0x4A4B4C4D
FRAME_TYPE_DATA   = 0x4A4B4C4E

# 关键帧大小阈值
I_FRAME_THRESHOLD = 30 * 1024  # 30 KB，更小的阈值加速关键帧检测

class P2PControllerApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("H264 Client - Ultra Low Latency")
        self.root.geometry("400x300")
        
        self.udp_socket = None
        self.target_addr = None 
        self.connected = False
        self.running = True
        
        self.frame_buffers = {}
        self.frame_lock = _threading.Lock()  # 使用更快的锁
        
        # === 极致优化：直接使用 numpy 数组 ===
        self.latest_frame = None  # 存储原始 numpy 数组
        self.frame_width = 0
        self.frame_height = 0
        self.image_lock = _threading.Lock()
        
        # === 帧丢弃计数器 ===
        self.frame_drops = 0
        self.total_frames = 0
        self.last_frame_time = 0
        
        # === 分辨率设置 ===
        self.resolution_presets = {
            '480p': (640, 480),
            '720p': (1280, 720),
            '1080p': (1920, 1080)
        }
        self.selected_resolution = '720p'  # 默认 720p
        
        # === 渲染优化 ===
        self.last_render_time = 0
        self.render_interval = 0.033  # 30 FPS 渲染限制
        
        # === 详细性能监控 ===
        self.perf_stats = {
            'decode_times': [],
            'scale_times': [],
            'render_times': [],
            'network_times': [],
            'total_times': [],
            'frame_sizes': [],
            'fps': []
        }
        self.last_stats_time = 0
        self.frame_count = 0
        
        # === 基本设置 ===
        self.hw_accel_type = "Software"  # 简化为软件解码
        
        # GUI
        self.frame_login = tk.Frame(self.root)
        self.frame_login.pack(pady=40)
        
        tk.Label(self.frame_login, text="远程桌面 P2P", font=("Arial", 16, "bold")).pack(pady=10)
        self.entry_code = tk.Entry(self.frame_login, width=20, font=("Arial", 12))
        self.entry_code.pack(pady=5)
        self.btn_connect = tk.Button(self.frame_login, text="连接", command=self.start_p2p, width=15, bg="#4CAF50", fg="white")
        self.btn_connect.pack(pady=15)
        
        self.status_label = tk.Label(self.root, text=f"端口 {LOCAL_UDP_PORT}", fg="gray")
        self.status_label.pack()
        
        self.canvas = None
        self.canvas_image_obj = None
        
    def start_p2p(self):
        code = self.entry_code.get()
        if not code: return
        self.btn_connect.config(state=tk.DISABLED)
        threading.Thread(target=self.p2p_worker, args=(code,), daemon=True).start()

    def p2p_worker(self, code):
        try:
            tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tcp_sock.connect((SERVER_IP, SERVER_PORT))
            tcp_sock.send(json.dumps({'type': 'connect', 'code': code}).encode())
            resp = tcp_sock.recv(1024).decode()
            tcp_sock.close()
            msg = json.loads(resp)
            
            if msg.get('status') == 'found':
                self.target_addr = (msg['peer_ip'], int(msg['peer_port']))
                self.update_status(f"打洞...")
                
                # === 低延迟网络设置 ===
                self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                # 增大接收缓冲区
                self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8*1024*1024)
                # 增大发送缓冲区
                self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8*1024*1024)
                # 启用地址重用
                self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                
                try: self.udp_socket.bind(('0.0.0.0', LOCAL_UDP_PORT))
                except OSError: self.udp_socket.bind(('0.0.0.0', 0))
                
                threading.Thread(target=self.recv_loop, daemon=True).start()
                
                # === 快速打洞 ===
                punch_msg = json.dumps({'type': 'punch'}).encode()
                for _ in range(100):  # 减少打洞次数，加快连接
                    self.udp_socket.sendto(punch_msg, self.target_addr)
                    time.sleep(0.01)  # 减少等待时间
                    if self.connected:
                        break
                if not self.connected:
                    self.update_status("失败")
                    self.btn_connect.config(state=tk.NORMAL)
            else:
                self.update_status("未找到")
                self.btn_connect.config(state=tk.NORMAL)
        except Exception as e:
            self.update_status(f"错误: {e}")
            self.btn_connect.config(state=tk.NORMAL)

    def recv_loop(self):
        codec = None
        
        # === 低延迟网络设置 ===
        self.udp_socket.settimeout(0.5)  # 减少超时等待
        # 增大接收缓冲区，减少丢包
        self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8*1024*1024)
        
        latest_fid = -1
        current_fid = -1
        
        empty_frame_count = 0
        
        print("[接收线程] 等待关键帧...")
        print(f"[接收线程] 解码方式: {self.hw_accel_type}")
        
        # === 性能监控 ===
        last_network_time = time.time()
        
        while self.running:
            try:
                data, addr = self.udp_socket.recvfrom(65535)
                
                if data == b"PUNCH_OK":
                    if not self.connected:
                        self.connected = True
                        self.target_addr = addr 
                        self.root.after(0, self.show_desktop_ui)
                        threading.Thread(target=self.heartbeat_daemon, daemon=True).start()
                    continue
                
                if not self.connected: continue
                
                if len(data) < 4: continue
                pkt_type = struct.unpack('!I', data[:4])[0]
                
                if pkt_type == FRAME_TYPE_HEADER:
                    if len(data) < 12: continue
                    fid, total_size = struct.unpack('!II', data[4:12])
                    
                    # === 低延迟优化：丢弃旧帧 ===
                    # 如果收到的帧ID比当前处理的帧ID大很多，说明中间有帧丢失
                    # 直接跳转到最新帧，丢弃中间的帧
                    if fid > latest_fid + 10:
                        print(f"[丢弃] 跳帧: {latest_fid} -> {fid}")
                        latest_fid = fid
                        with self.frame_lock:
                            self.frame_buffers = {fid: {'data': bytearray(total_size), 'recvd_len': 0, 'total_len': total_size}}
                    elif fid != latest_fid:
                        with self.frame_lock:
                            self.frame_buffers = {fid: {'data': bytearray(total_size), 'recvd_len': 0, 'total_len': total_size}}
                        latest_fid = fid
                    continue

                elif pkt_type == FRAME_TYPE_DATA:
                    if len(data) < 14: continue
                    fid, total_size, chunk_idx = struct.unpack('!IIH', data[4:14])
                    chunk_data = data[14:]
                    
                    if fid == latest_fid:
                        with self.frame_lock:
                            if fid not in self.frame_buffers: continue
                            obj = self.frame_buffers[fid]
                            
                            start_pos = chunk_idx * MTU_SIZE
                            if start_pos + len(chunk_data) <= len(obj['data']):
                                obj['data'][start_pos : start_pos + len(chunk_data)] = chunk_data
                                obj['recvd_len'] += len(chunk_data)
                            
                            if obj['recvd_len'] >= obj['total_len']:
                                # === 首帧过滤逻辑 ===
                                if codec is None and obj['total_len'] < I_FRAME_THRESHOLD:
                                    # print(f"[丢弃] 等待关键帧，当前: {obj['total_len']} bytes")
                                    if fid in self.frame_buffers: del self.frame_buffers[fid]
                                    continue
                                
                                # === 初始化解码器 ===
                                if codec is None:
                                    print(f"[初始化] 关键帧 {obj['total_len']} bytes")
                                    codec = av.CodecContext.create('h264', 'r')
                                    codec.thread_type = 'auto'
                                
                                try:
                                    # === 性能监控：网络时间 ===
                                    network_time = time.time() - last_network_time
                                    self.perf_stats['network_times'].append(network_time)
                                    last_network_time = time.time()
                                    
                                    # === 极致优化：仅处理最新帧 ===
                                    # 如果有更新的帧到达，当前帧可能已经过时
                                    if fid < latest_fid - 1:
                                        if fid in self.frame_buffers: del self.frame_buffers[fid]
                                        self.frame_drops += 1
                                        continue
                                    
                                    # === 性能监控：解码开始 ===
                                    decode_start = time.time()
                                    
                                    packet = av.packet.Packet(bytes(obj['data']))
                                    frames = codec.decode(packet)
                                    
                                    decode_time = time.time() - decode_start
                                    self.perf_stats['decode_times'].append(decode_time)
                                    
                                    # === Windows性能优化：限制解码频率 ===
                                    if decode_time > 0.05 and platform.system() == 'Windows':
                                        # 解码时间过长，给系统喘息时间
                                        time.sleep(0.01)
                                        if fid in self.frame_buffers: del self.frame_buffers[fid]
                                        self.frame_drops += 1
                                        continue
                                    
                                    if frames:
                                        empty_frame_count = 0
                                        frame = frames[0]
                                        
                                        # === 性能监控：转换时间 ===
                                        convert_start = time.time()
                                        
                                        # === 极致优化：直接存储 numpy 数组 ===
                                        # 跳过 PIL 转换，直接存储原始 RGB 数据
                                        img = frame.to_ndarray(format='rgb24')
                                        
                                        convert_time = time.time() - convert_start
                                        
                                        with self.image_lock:
                                            self.latest_frame = img
                                            self.frame_width = frame.width
                                            self.frame_height = frame.height
                                        
                                        # === 统计信息 ===
                                        self.total_frames += 1
                                        self.frame_count += 1
                                        
                                        # === 性能监控：定期输出 ===
                                        current_time = time.time()
                                        if current_time - self.last_stats_time > 2.0:
                                            self.print_perf_stats()
                                            self.last_stats_time = current_time
                                            
                                        # 记录帧大小
                                        self.perf_stats['frame_sizes'].append(len(obj['data']))
                                    else:
                                        empty_frame_count += 1
                                        if empty_frame_count > 50:
                                            codec = av.CodecContext.create('h264', 'r')
                                            empty_frame_count = 0
                                except Exception:
                                    pass
                                finally:
                                    if fid in self.frame_buffers:
                                        del self.frame_buffers[fid]
                        continue
                        
            except: pass
                
        self.connected = False

    def heartbeat_daemon(self):
        while self.running and self.connected:
            self.udp_socket.sendto(b"HEARTBEAT", self.target_addr)
            time.sleep(1.0)

    def show_desktop_ui(self):
        # 根据选择的分辨率设置窗口大小
        width, height = self.resolution_presets[self.selected_resolution]
        self.root.geometry(f"{width}x{height}")
        self.frame_login.pack_forget()
        self.status_label.pack_forget()
        
        # 顶部栏默认隐藏
        self.top_bar_visible = False
        
        # 创建画布（占据整个窗口）
        self.canvas = tk.Canvas(self.root, bg='black', highlightthickness=0, cursor="arrow")
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas_image_obj = self.canvas.create_image(0, 0, anchor=tk.NW)
        
        # 顶部栏（悬浮显示，默认隐藏）
        self.top_bar = tk.Frame(self.root, bg="#333", height=30, relief=tk.RAISED, bd=1)
        self.status_label_top = tk.Label(self.top_bar, text=f"Connected - {self.selected_resolution}", bg="#333", fg="white")
        self.status_label_top.pack(side=tk.LEFT, padx=10)
        
        # 添加退出按钮
        self.exit_btn = tk.Button(self.top_bar, text="Exit", command=self.on_close, bg="#d32f2f", fg="white", bd=0)
        self.exit_btn.pack(side=tk.RIGHT, padx=10)
        
        # 分辨率按钮框架
        self.res_frame = tk.Frame(self.top_bar, bg="#333")
        self.res_buttons = {}
        
        # 添加分辨率选择按钮
        def set_resolution(res):
            self.selected_resolution = res
            width, height = self.resolution_presets[res]
            # 计算新窗口位置，保持居中
            screen_width = self.root.winfo_screenwidth()
            screen_height = self.root.winfo_screenheight()
            x = (screen_width - width) // 2
            y = (screen_height - height) // 2
            self.root.geometry(f"{width}x{height}+{x}+{y}")
            # 更新状态栏
            self.status_label_top.config(text=f"Connected - {res}")
            # 更新按钮样式
            for r, btn in self.res_buttons.items():
                btn.config(bg="#555" if r != res else "#4CAF50")
            # 发送分辨率设置到被控端
            if self.connected:
                cmd = json.dumps({'type': 'resolution', 'width': width, 'height': height})
                self.udp_socket.sendto(cmd.encode(), self.target_addr)
            print(f"[设置] 分辨率: {res}")
        
        # 创建分辨率按钮
        for res in ['480p', '720p', '1080p']:
            btn = tk.Button(self.res_frame, text=res, 
                           command=lambda r=res: set_resolution(r),
                           bg="#555" if res != self.selected_resolution else "#4CAF50", 
                           fg="white", bd=0, padx=8, pady=2)
            btn.pack(side=tk.LEFT, padx=2)
            self.res_buttons[res] = btn
        
        # 分辨率按钮默认显示
        self.res_frame.pack(side=tk.RIGHT, padx=10)
        
        # 顶部栏自动隐藏/显示功能
        def toggle_top_bar():
            if self.top_bar_visible:
                # 隐藏顶部栏
                self.top_bar.place_forget()
                self.top_bar_visible = False
                print("[UI] 顶部栏已隐藏")
            else:
                # 显示顶部栏（悬浮在窗口上方）
                self.top_bar.place(x=0, y=0, relwidth=1, height=30)
                # 提升顶部栏到最上层
                self.top_bar.lift()
                self.top_bar_visible = True
                print("[UI] 顶部栏已显示")
        
        # 鼠标悬停顶部边缘检测和鼠标移动处理
        def on_motion(event):
            # 处理顶部栏显示
            if event.y < 20 and not self.top_bar_visible:
                toggle_top_bar()
            # 处理鼠标移动
            if self.connected:
                cmd = json.dumps({'type': 'mouse', 'x': event.x, 'y': event.y})
                self.udp_socket.sendto(cmd.encode(), self.target_addr)
        
        def on_top_bar_leave(event):
            # 当鼠标离开顶部栏时，延迟1秒隐藏
            self.root.after(1000, lambda:
                toggle_top_bar() if self.top_bar_visible and not self.top_bar.winfo_containing(self.root.winfo_pointerx(), self.root.winfo_pointery()) else None
            )
        
        # 绑定事件
        self.canvas.bind("<Motion>", on_motion)
        self.top_bar.bind("<Leave>", on_top_bar_leave)
        self.canvas.bind("<Button-1>", self.send_mouse_click)
        
        # 初始隐藏顶部栏
        self.top_bar.place_forget()
        
        self.render_loop()
        print(f"[提示] 当前分辨率: {self.selected_resolution}")
        print("[提示] 鼠标悬停顶部显示分辨率切换")

    def on_mouse_move(self, event):
        # 这个方法可能不再被直接调用，因为我们已经在on_motion中处理了鼠标移动
        if self.connected:
            cmd = json.dumps({'type': 'mouse', 'x': event.x, 'y': event.y})
            self.udp_socket.sendto(cmd.encode(), self.target_addr)

    def send_mouse_click(self, event):
        if self.connected:
            cmd = json.dumps({'type': 'mouse', 'x': event.x, 'y': event.y, 'click': True})
            self.udp_socket.sendto(cmd.encode(), self.target_addr)

    def render_loop(self):
        if not self.running: return
        
        current_time = time.time()
        render_start = current_time
        
        try:
                # === 极速优化：只在有新帧时渲染 ===
                with self.image_lock:
                    if self.latest_frame is not None:
                        # === 立即清除帧引用，避免阻塞解码线程 ===
                        frame = self.latest_frame
                        width = self.frame_width
                        height = self.frame_height
                        
                        # 立即清除引用，让解码线程可以继续
                        self.latest_frame = None
                    else:
                        # 没有新帧，跳过渲染
                        self.root.after(16, self.render_loop)  # 增加间隔到16ms
                        return
                
                # === 固定分辨率渲染 ===
                canvas_w = self.canvas.winfo_width()
                canvas_h = self.canvas.winfo_height()
                
                if canvas_w <= 1 or width <= 0:
                    self.root.after(16, self.render_loop)
                    return
                
                # 不考虑顶部栏，使用完整窗口空间
                available_height = canvas_h
                available_y = 0
                
                # 使用选择的固定分辨率
                target_width, target_height = self.resolution_presets[self.selected_resolution]
                
                # 计算缩放比例，确保画面完整显示
                ratio = min(canvas_w / width, available_height / height)
                new_w = int(width * ratio)
                new_h = int(height * ratio)
                
                # === 性能监控：缩放开始 ===
                scale_start = time.time()
                
                # === 固定性能模式：始终使用快速切片 ===
                if new_w != width or new_h != height:
                    # 始终使用高性能切片方法
                    step_x = max(1, int(width / new_w))
                    step_y = max(1, int(height / new_h))
                    # 直接使用切片，避免 np.ix_ 的开销
                    resized_frame = frame[::step_y, ::step_x]
                    # 如果尺寸不匹配，手动调整
                    if resized_frame.shape[0] != new_h or resized_frame.shape[1] != new_w:
                        resized_frame = resized_frame[:new_h, :new_w]
                else:
                    resized_frame = frame
                
                scale_time = time.time() - scale_start
                self.perf_stats['scale_times'].append(scale_time)
                
                # === 极致优化：内存复用 ===
                # 避免频繁创建新的 PhotoImage 对象
                header = f'P6 {resized_frame.shape[1]} {resized_frame.shape[0]} 255\n'.encode()
                data = header + resized_frame.tobytes()
                
                # === 安全的内存管理 ===
                try:
                    # 先删除旧的图像对象
                    if hasattr(self, 'canvas_img'):
                        del self.canvas_img
                    
                    # 创建新的 PhotoImage
                    self.canvas_img = tk.PhotoImage(data=data)
                    
                    # 更新画布
                    self.canvas.itemconfig(self.canvas_image_obj, image=self.canvas_img)
                    self.canvas.coords(self.canvas_image_obj, 
                                      (canvas_w - resized_frame.shape[1])//2, 
                                      (available_height - resized_frame.shape[0])//2)
                except Exception as img_err:
                    pass
                
                render_time = time.time() - render_start
                self.perf_stats['render_times'].append(render_time)
                
                # === 性能优化：立即释放大内存 ===
                # 删除临时变量，减少内存占用
                del resized_frame, data, header
            
        except Exception as e:
            pass
        
        # === Windows最速渲染优化 ===
        current_time = time.time()
        elapsed = current_time - self.last_render_time
        
        # 根据性能自动调整渲染频率
        if elapsed > 0.05:  # 如果渲染时间超过50ms
            self.render_interval = 0.05  # 降低到20 FPS
        elif elapsed < 0.02:  # 如果渲染时间少于20ms
            self.render_interval = 0.033  # 提高到30 FPS
        
        # 增加最小间隔，减少CPU占用
        min_interval = max(16, int(self.render_interval * 1000))  # 最小16ms (60 FPS)
        
        # Windows特定优化：使用时间片调度
        if platform.system() == 'Windows':
            # 给系统更多时间处理其他任务
            min_interval = max(min_interval, 20)
        
        self.root.after(min_interval, self.render_loop)
        self.last_render_time = current_time
        
        # === Windows内存优化 ===
        if platform.system() == 'Windows':
            # 强制垃圾回收，减少内存碎片
            import gc
            gc.collect()

    def print_perf_stats(self):
        """打印详细的性能统计信息"""
        if not self.perf_stats['decode_times']:
            return
        
        # 计算平均值
        avg_decode = sum(self.perf_stats['decode_times']) / len(self.perf_stats['decode_times']) * 1000
        avg_scale = sum(self.perf_stats['scale_times']) / len(self.perf_stats['scale_times']) * 1000
        avg_render = sum(self.perf_stats['render_times']) / len(self.perf_stats['render_times']) * 1000
        avg_network = sum(self.perf_stats['network_times']) / len(self.perf_stats['network_times']) * 1000 if self.perf_stats['network_times'] else 0
        avg_frame_size = sum(self.perf_stats['frame_sizes']) / len(self.perf_stats['frame_sizes']) / 1024 if self.perf_stats['frame_sizes'] else 0
        
        # 计算FPS
        fps = self.frame_count / 2.0  # 每2秒统计一次
        
        # 计算总延迟
        total_latency = avg_decode + avg_scale + avg_render + avg_network
        
        # 打印详细统计
        print(f"[性能监控] FPS: {fps:.1f}, 总延迟: {total_latency:.1f}ms")
        print(f"[性能监控] 解码: {avg_decode:.1f}ms, 缩放: {avg_scale:.1f}ms, 渲染: {avg_render:.1f}ms, 网络: {avg_network:.1f}ms")
        print(f"[性能监控] 平均帧大小: {avg_frame_size:.1f}KB, 丢帧率: {self.frame_drops/self.total_frames*100:.1f}%")
        print(f"[性能监控] 解码方式: {self.hw_accel_type}")
        
        # 性能警告
        if avg_render > 30:
            print(f"[警告] 渲染延迟过高: {avg_render:.1f}ms (建议: 降低分辨率)")
        if avg_network > 40:
            print(f"[警告] 网络延迟过高: {avg_network:.1f}ms (建议: 检查网络连接)")
        
        # 重置统计
        self.perf_stats = {
            'decode_times': [],
            'scale_times': [],
            'render_times': [],
            'network_times': [],
            'total_times': [],
            'frame_sizes': [],
            'fps': []
        }
        self.frame_count = 0

    def update_status(self, text): self.status_label.config(text=text)
    def run(self): 
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.mainloop()
    def on_close(self):
        self.running = False
        try:
            self.root.destroy()
        except:
            pass

if __name__ == "__main__":
    app = P2PControllerApp()
    app.run()