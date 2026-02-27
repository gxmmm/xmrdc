import socket
import threading
import json
import time
import tkinter as tk
import struct
import threading as _threading
import platform
import gc
import os
import ctypes
import mmap

# 处理DPI缩放问题
if platform.system() == 'Windows':
    try:
        # 设置DPI感知
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        print("[优化] Windows DPI感知已设置为每监视器感知")
    except Exception as e:
        print(f"[优化] Windows DPI感知设置失败: {e}")

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
        
        # === 共享内存设置 ===
        self.shm_name = "xmrdc_video_shm"
        self.shm_size = 10 * 1024 * 1024  # 10MB共享内存
        self.shm = None
        self.shm_buffer = None
        self.shm_lock = _threading.Lock()
        self.cpp_process = None
        
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
        
        # === 性能监控 ===
        self.perf_stats = {
            'network_times': [],
            'frame_sizes': [],
            'fps': []
        }
        self.last_stats_time = 0
        self.frame_count = 0
        self.total_frames = 0
        self.frame_drops = 0
        
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
        self.canvas_window_id = None  # 用于C++渲染的窗口句柄
        
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
        # === 低延迟网络设置 ===
        self.udp_socket.settimeout(0.5)  # 减少超时等待
        # 增大接收缓冲区，减少丢包
        self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8*1024*1024)
        
        latest_fid = -1
        current_fid = -1
        
        print("[接收线程] 等待关键帧...")
        print("[接收线程] 使用共享内存传输H264数据到C++")
        
        # === 性能监控 ===
        last_network_time = time.time()
        
        while self.running:
            try:
                data, addr = self.udp_socket.recvfrom(65535)
                
                if data == b"PUNCH_OK":
                    if not self.connected:
                        self.connected = True
                        self.target_addr = addr 
                        print(f"[Python] Connection established with {addr}")
                        self.root.after(0, self.show_desktop_ui)
                        threading.Thread(target=self.heartbeat_daemon, daemon=True).start()
                    continue
                
                if not self.connected:
                    print(f"[Python] Received data but not connected yet: {len(data)} bytes from {addr}")
                    continue
                
                if len(data) < 4: continue
                pkt_type = struct.unpack('!I', data[:4])[0]
                
                if pkt_type == FRAME_TYPE_HEADER:
                    if len(data) < 14: continue
                    fid, total_size, total_chunks = struct.unpack('!IIH', data[4:14])
                    
                    print(f"[Python] Received FRAME_TYPE_HEADER: fid={fid}, total_size={total_size} bytes, total_chunks={total_chunks}")
                    
                    # === 低延迟优化：丢弃旧帧 ===
                    # 如果收到的帧ID比当前处理的帧ID大很多，说明中间有帧丢失
                    # 直接跳转到最新帧，丢弃中间的帧
                    if fid > latest_fid + 10:
                        print(f"[丢弃] 跳帧: {latest_fid} -> {fid}")
                        latest_fid = fid
                        with self.frame_lock:
                            self.frame_buffers = {fid: {'data': bytearray(total_size), 'recvd_len': 0, 'total_len': total_size, 'total_chunks': total_chunks, 'received_chunks': set(), 'timestamp': time.time()}}
                            print(f"[Python] Created new frame buffer for fid={fid}")
                    elif fid != latest_fid:
                        with self.frame_lock:
                            self.frame_buffers = {fid: {'data': bytearray(total_size), 'recvd_len': 0, 'total_len': total_size, 'total_chunks': total_chunks, 'received_chunks': set(), 'timestamp': time.time()}}
                            print(f"[Python] Created frame buffer for fid={fid}")
                        latest_fid = fid
                    continue

                elif pkt_type == FRAME_TYPE_DATA:
                    if len(data) < 16: continue
                    fid, total_size, chunk_idx, total_chunks = struct.unpack('!IIHH', data[4:16])
                    chunk_data = data[16:]
                    
                    print(f"[Python] Received FRAME_TYPE_DATA: fid={fid}, chunk_idx={chunk_idx}/{total_chunks}, chunk_size={len(chunk_data)} bytes")
                    
                    if fid == latest_fid:
                        with self.frame_lock:
                            if fid not in self.frame_buffers:
                                print(f"[Python] fid={fid} not in frame_buffers, skipping")
                                continue
                            obj = self.frame_buffers[fid]
                            
                            # 检查分片是否已接收（避免重复）
                            if chunk_idx in obj['received_chunks']:
                                continue
                            
                            start_pos = chunk_idx * MTU_SIZE
                            if start_pos + len(chunk_data) <= len(obj['data']):
                                obj['data'][start_pos : start_pos + len(chunk_data)] = chunk_data
                                obj['recvd_len'] += len(chunk_data)
                                obj['received_chunks'].add(chunk_idx)
                                print(f"[Python] Updated frame {fid}: recvd_len={obj['recvd_len']}/{obj['total_len']} bytes, chunks={len(obj['received_chunks'])}/{obj['total_chunks']}")
                            else:
                                print(f"[Python] Chunk out of bounds: start_pos={start_pos}, chunk_size={len(chunk_data)}, buffer_size={len(obj['data'])}")
                            
                            # 检查是否接收完整（基于分片数和总大小）
                            if len(obj['received_chunks']) == obj['total_chunks'] and obj['recvd_len'] >= obj['total_len']:
                                print(f"[Python] Received complete frame: fid={fid}, size={obj['total_len']} bytes, chunks={len(obj['received_chunks'])}")
                                # === 写入共享内存 ===
                                try:
                                    self.write_to_shared_memory(obj['data'], fid, obj['total_len'])
                                    
                                    # === 统计信息 ===
                                    self.total_frames += 1
                                    self.frame_count += 1
                                    print(f"[Python] Frame count: {self.frame_count}, total frames: {self.total_frames}")
                                    
                                    # === 性能监控：定期输出 ===
                                    current_time = time.time()
                                    if current_time - self.last_stats_time > 2.0:
                                        self.print_perf_stats()
                                        self.last_stats_time = current_time
                                        
                                    # 记录帧大小
                                    self.perf_stats['frame_sizes'].append(obj['total_len'])
                                except Exception as shm_err:
                                    print(f"[共享内存错误] {shm_err}")
                                finally:
                                    if fid in self.frame_buffers:
                                        del self.frame_buffers[fid]
                                        print(f"[Python] Cleared frame buffer for fid={fid}")
                        continue
                        
            except: pass
                
        self.connected = False
    
    def write_to_shared_memory(self, h264_data, fid, total_len):
        """将H264数据写入共享内存"""
        print(f"[Python] write_to_shared_memory called: fid={fid}, total_len={total_len}, data_size={len(h264_data)} bytes")
        
        with self.shm_lock:
            if self.shm is None:
                print("[Python] shm is None, skipping write")
                return
            
            # 计算总数据大小：4字节data_size + 8字节header + 数据长度
            data_size = 4 + 8 + len(h264_data)
            
            print(f"[Python] Writing to shared memory: header_size=8 bytes, total_size={data_size} bytes")
            
            # 检查共享内存是否有足够空间
            if data_size > self.shm_size:
                print(f"[共享内存] 数据过大: {data_size} bytes (max: {self.shm_size} bytes)")
                return
            
            # 写入数据
            try:
                # 定位到共享内存开头
                self.shm.seek(0)
                
                # 写入data_size（4字节，网络字节序）
                self.shm.write(struct.pack('!I', data_size))
                
                # 写入fid（4字节，网络字节序）
                self.shm.write(struct.pack('!I', fid))
                
                # 写入total_len（4字节，网络字节序）
                self.shm.write(struct.pack('!I', total_len))
                
                # 写入H264数据
                self.shm.write(h264_data)
                
                # 确保数据被写入
                self.shm.flush()
                
                print(f"[Python] Successfully wrote frame {fid} to shared memory")
            except Exception as e:
                print(f"[Python] Error writing to shared memory: {e}")

    def heartbeat_daemon(self):
        while self.running and self.connected:
            self.udp_socket.sendto(b"HEARTBEAT", self.target_addr)
            time.sleep(1.0)


    def test_shared_memory(self):
        """测试共享内存读取"""
        print("[Python] Starting shared memory test...")
        
        with self.shm_lock:
            if self.shm is None:
                print("[Python] shm is None, skipping test")
                return
            
            # 读取测试数据
            self.shm.seek(0)
            data = self.shm.read(1024)
            if len(data) >= 12:
                data_size = struct.unpack('!I', data[:4])[0]
                fid = struct.unpack('!I', data[4:8])[0]
                frame_size = struct.unpack('!I', data[8:12])[0]
                
                if data_size > 12 and len(data) >= 12 + frame_size:
                    test_data = data[12:12+frame_size].decode('utf-8', errors='ignore')
                else:
                    test_data = ""
                
                print(f"[Python] Read test data: data_size={data_size}, fid={fid}, frame_size={frame_size}, data='{test_data}'")
                
                # 检查是否是C++端的测试数据
                if fid == 9999 and test_data == "HELLO_TEST":
                    print("[Python] Shared memory test PASSED! Received C++ test data.")
                    
                    # 回复测试数据
                    reply_data = "PYTHON_REPLY"
                    reply_size = 8 + len(reply_data)
                    header = struct.pack('!II', 8888, len(reply_data))
                    full_reply = struct.pack('!I', reply_size) + header + reply_data.encode()
                    
                    self.shm.seek(0)
                    self.shm.write(full_reply)
                    self.shm.flush()
                    
                    print("[Python] Sent reply to C++: 'PYTHON_REPLY'")
                else:
                    print("[Python] No C++ test data found, waiting...")
            else:
                print("[Python] Not enough data in shared memory for test")

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
        
        # 获取窗口句柄用于C++渲染
        self.canvas_window_id = self.canvas.winfo_id()
        print(f"[UI] 窗口句柄: {self.canvas_window_id}")
        
        # 初始化共享内存
        self.init_shared_memory()

        # 测试共享内存
        threading.Thread(target=self.test_shared_memory, daemon=True).start()
        
        # 启动C++渲染进程
        self.start_cpp_renderer()
        
        # 立即发送分辨率设置到被控端
        if self.connected:
            cmd = json.dumps({'type': 'resolution', 'width': width, 'height': height})
            self.udp_socket.sendto(cmd.encode(), self.target_addr)
            print(f"[UI] 已发送分辨率设置: {width}x{height}")
        
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
            self.update_resolution(res)
        
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
        
        print(f"[提示] 当前分辨率: {self.selected_resolution}")
        print("[提示] 鼠标悬停顶部显示分辨率切换")
        print("[提示] C++渲染进程已启动")
        print("[提示] 顶部栏由Python处理，C++只负责远程桌面画面")

    def init_shared_memory(self):
        """初始化共享内存"""
        try:
            # Windows共享内存
            if platform.system() == 'Windows':
                # 创建共享内存
                self.shm = mmap.mmap(-1, self.shm_size, tagname=self.shm_name)
                print(f"[共享内存] 已创建: {self.shm_name}, 大小: {self.shm_size} bytes")
            else:
                # Linux共享内存
                self.shm = mmap.mmap(-1, self.shm_size)
                print(f"[共享内存] 已创建, 大小: {self.shm_size} bytes")
        except Exception as e:
            print(f"[共享内存] 初始化失败: {e}")
            self.shm = None
    
    def start_cpp_renderer(self):
        """启动C++渲染进程"""
        try:
            # 获取窗口句柄
            window_handle = self.canvas_window_id
            width, height = self.resolution_presets[self.selected_resolution]
            
            # 启动C++渲染进程
            import subprocess
            cpp_executable = "xmrdc_renderer_mf.exe"  # C++可执行文件名
            
            # 传递窗口句柄和共享内存名称
            cmd = [
                cpp_executable,
                str(window_handle),
                self.shm_name,
                str(width),
                str(height)
            ]
            
            self.cpp_process = subprocess.Popen(cmd)
            print(f"[C++渲染] 进程已启动, PID: {self.cpp_process.pid}")
            print(f"[C++渲染] 窗口句柄: {window_handle}")
            print(f"[C++渲染] 共享内存: {self.shm_name}")
            print(f"[C++渲染] 分辨率: {width}x{height}")
        except Exception as e:
            print(f"[C++渲染] 启动失败: {e}")
            self.cpp_process = None

    def send_mouse_click(self, event):
        if self.connected:
            cmd = json.dumps({'type': 'mouse', 'x': event.x, 'y': event.y, 'click': True})
            self.udp_socket.sendto(cmd.encode(), self.target_addr)

    def update_resolution(self, res):
        """更新分辨率并通知C++进程"""
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
        
        # 通知C++进程更新分辨率
        if self.cpp_process and self.cpp_process.poll() is None:
            # 通过共享内存发送分辨率更新命令
            self.send_resolution_to_cpp(width, height)
        
        print(f"[设置] 分辨率: {res}")
    
    def send_resolution_to_cpp(self, width, height):
        """发送分辨率更新到C++进程"""
        with self.shm_lock:
            if self.shm is None:
                return
            
            # 使用特殊命令标记分辨率更新
            # 命令格式: 4字节data_size + 4字节命令(0xFFFFFFFF) + 4字节宽度 + 4字节高度
            # 总大小为16字节
            data_size = 12  # 4字节命令 + 4字节宽度 + 4字节高度
            self.shm.seek(0)
            # 写入数据大小
            self.shm.write(struct.pack('!I', data_size))
            # 写入命令
            self.shm.write(struct.pack('!I', 0xFFFFFFFF))
            # 写入宽度和高度
            self.shm.write(struct.pack('!II', width, height))
            self.shm.flush()
            print(f"[Python] Sent resolution update to C++: {width}x{height}")

    def print_perf_stats(self):
        """打印详细的性能统计信息"""
        if not self.perf_stats['network_times']:
            return
        
        # 计算平均值
        avg_network = sum(self.perf_stats['network_times']) / len(self.perf_stats['network_times']) * 1000 if self.perf_stats['network_times'] else 0
        avg_frame_size = sum(self.perf_stats['frame_sizes']) / len(self.perf_stats['frame_sizes']) / 1024 if self.perf_stats['frame_sizes'] else 0
        
        # 计算FPS
        fps = self.frame_count / 2.0  # 每2秒统计一次
        
        # 打印详细统计
        print(f"[性能监控] FPS: {fps:.1f}")
        print(f"[性能监控] 网络: {avg_network:.1f}ms, 平均帧大小: {avg_frame_size:.1f}KB")
        print(f"[性能监控] 丢帧率: {self.frame_drops/self.total_frames*100:.1f}%")
        
        # 重置统计
        self.perf_stats = {
            'network_times': [],
            'frame_sizes': [],
            'fps': []
        }
        self.frame_count = 0

    def update_status(self, text): self.status_label.config(text=text)
    def run(self): 
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.mainloop()
    def on_close(self):
        # 停止C++渲染进程
        if self.cpp_process and self.cpp_process.poll() is None:
            try:
                self.cpp_process.terminate()
                self.cpp_process.wait(timeout=2)
                print("[C++渲染] 进程已停止")
            except:
                try:
                    self.cpp_process.kill()
                    print("[C++渲染] 进程已强制停止")
                except:
                    pass
        
        # 关闭共享内存
        if self.shm:
            try:
                self.shm.close()
                print("[共享内存] 已关闭")
            except:
                pass
        
        self.running = False
        try:
            self.root.destroy()
        except:
            pass

if __name__ == "__main__":
    app = P2PControllerApp()
    app.run()