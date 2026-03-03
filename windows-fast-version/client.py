import socket
import threading
import json
import time
import struct
import av
import platform
import gc
import os
import numpy as np
import pyglet
from pyglet import gl
from pyglet.window import key, mouse, FPSDisplay

# Windows特定优化
if platform.system() == 'Windows':
    try:
        import ctypes
        import psutil
        kernel32 = ctypes.windll.kernel32
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), 0x00000003)
        kernel32.SetProcessWorkingSetSize(kernel32.GetCurrentProcess(), -1, -1)
        p = psutil.Process(os.getpid())
        if os.cpu_count() > 1:
            p.cpu_affinity([0, 1])
    except Exception:
        pass

# 配置常量
SERVER_IP = 'frp-hat.com'
SERVER_PORT = 43972
MTU_SIZE = 1400
LOCAL_UDP_PORT = 20000

FRAME_TYPE_HEADER = 0x4A4B4C4D
FRAME_TYPE_DATA   = 0x4A4B4C4E
I_FRAME_THRESHOLD = 30 * 1024

class P2PControllerApp:
    def __init__(self):
        # === 核心状态 ===
        self.running = True
        self.connected = False
        self.udp_socket = None
        self.target_addr = None
        
        # === 数据缓存 ===
        self.frame_buffers = {}
        self.frame_lock = threading.Lock()
        
        # === 图像数据 ===
        self.latest_frame = None
        self.image_lock = threading.Lock()
        
        # === Pyglet 窗口初始化 ===
        self.window = pyglet.window.Window(500, 400, caption="远程桌面 P2P", resizable=True)
        self.window.set_vsync(False) 
        
        # === GUI 组件 ===
        self.batch = pyglet.graphics.Batch()
        self.login_group = pyglet.graphics.Group()
        
        # 登录界面元素
        self.label_title = pyglet.text.Label("远程桌面 P2P", font_size=20, 
                                             x=250, y=300, anchor_x='center', batch=self.batch, group=self.login_group)
        
        self.label_code = pyglet.text.Label("口令:", font_size=14,
                                            x=150, y=220, anchor_x='right', batch=self.batch, group=self.login_group)
        
        self.text_input = ""
        self.label_input = pyglet.text.Label("", font_size=16, 
                                             x=160, y=220, anchor_x='left', batch=self.batch, group=self.login_group)
        
        self.input_bg = pyglet.shapes.Rectangle(160, 215, 180, 30, color=(240, 240, 240), batch=self.batch, group=self.login_group)
        
        self.btn_connect_rect = pyglet.shapes.Rectangle(200, 150, 100, 40, color=(76, 175, 80), batch=self.batch, group=self.login_group)
        self.btn_connect_label = pyglet.text.Label("连接", font_size=14, color=(255, 255, 255, 255),
                                                   x=250, y=170, anchor_x='center', batch=self.batch, group=self.login_group)
        
        self.status_label = pyglet.text.Label(f"端口 {LOCAL_UDP_PORT}", font_size=12, color=(128, 128, 128, 255),
                                              x=250, y=50, anchor_x='center', batch=self.batch, group=self.login_group)

        # === 桌面显示组件 ===
        self.texture = None
        self.sprite = None
        
        # === 分辨率预设与状态 ===
        self.resolution_presets = ['480p', '720p', '1080p']
        self.resolution_map = {'480p': (640, 480), '720p': (1280, 720), '1080p': (1920, 1080)}
        self.current_res_idx = 1  # 默认 720p
        
        self.pre_fullscreen_size = (1280, 720) # 记录全屏前的大小
        
        # === 性能监控 ===
        self.perf_stats = {'decode': [], 'render': []}
        self.fps_display = None
        
        # === 事件绑定 ===
        self.window.push_handlers(
            self.on_draw, 
            self.on_key_press, 
            self.on_key_release,
            self.on_mouse_press, 
            self.on_mouse_release,
            self.on_mouse_motion,
            self.on_mouse_drag
        )
        
        # 启动定时器
        pyglet.clock.schedule_interval(self.update_frame, 1/60.0)
        pyglet.clock.schedule_interval(self.update_stats, 2.0)

    # ==================== GUI 事件处理 ====================
    
    # --- 键盘事件 ---
    
    def on_key_press(self, symbol, modifiers):
        # 1. 登录界面
        if not self.connected:
            if symbol == key.BACKSPACE:
                self.text_input = self.text_input[:-1]
            elif symbol == key.ENTER:
                self.start_p2p()
            elif len(self.text_input) < 10:
                val = None
                if key._0 <= symbol <= key._9:
                    val = symbol - key._0
                elif key.NUM_0 <= symbol <= key.NUM_9:
                    val = symbol - key.NUM_0
                if val is not None:
                    self.text_input += str(val)
            self.label_input.text = self.text_input
            return

        # 2. 连接状态下的功能键
        if symbol == key.F11:
            # 循环切换分辨率
            self.current_res_idx = (self.current_res_idx + 1) % len(self.resolution_presets)
            new_res = self.resolution_presets[self.current_res_idx]
            self.change_resolution(new_res)
            return

        if symbol == key.F12:
            # 切换全屏
            self.toggle_fullscreen(not self.window.fullscreen)
            return
            
        if symbol == key.ESCAPE:
            if self.window.fullscreen:
                self.toggle_fullscreen(False)
                return

        # 3. 远程键盘转发
        # 发送键码，如果服务端 Python 处理，可以使用 symbol 名称；如果是 Windows API，需要虚拟键码
        # 这里发送 symbol 的名称 (如 'A', 'SPACE', 'ENTER')，服务端需适配
        try:
            k_name = pyglet.window.key.symbol_string(symbol)
            cmd = json.dumps({'type': 'key', 'key': k_name, 'action': 'press'})
            if self.target_addr: self.udp_socket.sendto(cmd.encode(), self.target_addr)
        except: pass

    def on_key_release(self, symbol, modifiers):
        if not self.connected: return
        # 远程键盘松开
        try:
            k_name = pyglet.window.key.symbol_string(symbol)
            cmd = json.dumps({'type': 'key', 'key': k_name, 'action': 'release'})
            if self.target_addr: self.udp_socket.sendto(cmd.encode(), self.target_addr)
        except: pass

    # --- 鼠标事件 ---

    def on_mouse_motion(self, x, y, dx, dy):
        if not self.connected or not self.target_addr or not self.texture: return
        
        # 实时鼠标移动发送
        vid_w, vid_h = self.texture.width, self.texture.height
        win_w, win_h = self.window.width, self.window.height
        ratio = min(win_w / vid_w, win_h / vid_h)
        display_w = vid_w * ratio
        display_h = vid_h * ratio
        offset_x = (win_w - display_w) / 2
        offset_y = (win_h - display_h) / 2
        
        # 检查是否在显示区域内
        if offset_x <= x <= offset_x + display_w and offset_y <= y <= offset_y + display_h:
            target_x = int((x - offset_x) / ratio)
            target_y = int((y - offset_y) / ratio)
            target_y = int(vid_h - target_y) # Y轴翻转
            
            cmd = json.dumps({'type': 'mouse', 'x': target_x, 'y': target_y})
            self.udp_socket.sendto(cmd.encode(), self.target_addr)

    def on_mouse_drag(self, x, y, dx, dy, buttons, modifiers):
        # 拖拽等同于移动，只是按住了键，实时发送坐标即可
        self.on_mouse_motion(x, y, dx, dy)

    def on_mouse_press(self, x, y, button, modifiers):
        if not self.connected:
            # 登录按钮点击
            if button == mouse.LEFT:
                if 200 <= x <= 300 and 150 <= y <= 190:
                    self.start_p2p()
            return

        # 远程点击转发
        if self.target_addr and self.texture:
            vid_w, vid_h = self.texture.width, self.texture.height
            win_w, win_h = self.window.width, self.window.height
            ratio = min(win_w / vid_w, win_h / vid_h)
            display_w = vid_w * ratio
            display_h = vid_h * ratio
            offset_x = (win_w - display_w) / 2
            offset_y = (win_h - display_h) / 2
            
            if offset_x <= x <= offset_x + display_w and offset_y <= y <= offset_y + display_h:
                target_x = int((x - offset_x) / ratio)
                target_y = int((y - offset_y) / ratio)
                target_y = int(vid_h - target_y)
                
                btn_name = "left" if button == mouse.LEFT else "right"
                cmd = json.dumps({'type': 'mouse', 'x': target_x, 'y': target_y, 'click': True, 'button': btn_name, 'action': 'press'})
                self.udp_socket.sendto(cmd.encode(), self.target_addr)

    def on_mouse_release(self, x, y, button, modifiers):
        if not self.connected: return
        
        # 远程点击松开
        if self.target_addr and self.texture:
            vid_w, vid_h = self.texture.width, self.texture.height
            win_w, win_h = self.window.width, self.window.height
            ratio = min(win_w / vid_w, win_h / vid_h)
            display_w = vid_w * ratio
            display_h = vid_h * ratio
            offset_x = (win_w - display_w) / 2
            offset_y = (win_h - display_h) / 2
            
            if offset_x <= x <= offset_x + display_w and offset_y <= y <= offset_y + display_h:
                target_x = int((x - offset_x) / ratio)
                target_y = int((y - offset_y) / ratio)
                target_y = int(vid_h - target_y)
                
                btn_name = "left" if button == mouse.LEFT else "right"
                cmd = json.dumps({'type': 'mouse', 'x': target_x, 'y': target_y, 'click': True, 'button': btn_name, 'action': 'release'})
                self.udp_socket.sendto(cmd.encode(), self.target_addr)

    # ==================== UI 辅助函数 ====================

    def toggle_fullscreen(self, enable):
        if enable:
            self.pre_fullscreen_size = (self.window.width, self.window.height)
            self.window.set_fullscreen(True)
        else:
            self.window.set_fullscreen(False)
            w, h = self.pre_fullscreen_size
            self.window.set_size(w, h)

    def on_draw(self):
        self.window.clear()
        
        if self.connected:
            if self.sprite:
                self.sprite.draw()
            if self.fps_display:
                self.fps_display.draw()
        else:
            self.batch.draw()

    # ==================== 网络逻辑 (基本不变) ====================

    def start_p2p(self):
        if not self.text_input: return
        code = self.text_input
        self.status_label.text = "正在连接..."
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
                pyglet.clock.schedule_once(lambda dt: self.update_status("打洞中..."), 0)
                
                self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8*1024*1024)
                self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8*1024*1024)
                self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                
                try: self.udp_socket.bind(('0.0.0.0', LOCAL_UDP_PORT))
                except OSError: self.udp_socket.bind(('0.0.0.0', 0))
                
                threading.Thread(target=self.recv_loop, daemon=True).start()
                
                punch_msg = json.dumps({'type': 'punch'}).encode()
                for _ in range(100):
                    self.udp_socket.sendto(punch_msg, self.target_addr)
                    time.sleep(0.01)
                    if self.connected: break
            else:
                pyglet.clock.schedule_once(lambda dt: self.update_status("未找到"), 0)
        except Exception as e:
            pyglet.clock.schedule_once(lambda dt: self.update_status(f"错误: {e}"), 0)

    def recv_loop(self):
        codec = None
        self.udp_socket.settimeout(0.5)
        latest_fid = -1
        
        print("[接收线程] 启动")
        
        while self.running:
            try:
                data, addr = self.udp_socket.recvfrom(65535)
                
                if data == b"PUNCH_OK":
                    if not self.connected:
                        self.connected = True
                        self.target_addr = addr
                        pyglet.clock.schedule_once(lambda dt: self.switch_to_desktop_ui(), 0)
                        threading.Thread(target=self.heartbeat_daemon, daemon=True).start()
                    continue
                
                if not self.connected: continue
                if len(data) < 4: continue
                
                pkt_type = struct.unpack('!I', data[:4])[0]
                
                if pkt_type == FRAME_TYPE_HEADER:
                    if len(data) < 12: continue
                    fid, total_size = struct.unpack('!II', data[4:12])
                    
                    if fid > latest_fid + 10:
                        latest_fid = fid
                    
                    if fid != latest_fid:
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
                                if codec is None and obj['total_len'] < I_FRAME_THRESHOLD:
                                    del self.frame_buffers[fid]
                                    continue
                                
                                if codec is None:
                                    print(f"[初始化] 解码器，关键帧: {obj['total_len']} B")
                                    codec = av.CodecContext.create('h264', 'r')
                                    codec.thread_type = 'auto'
                                
                                try:
                                    t_start = time.time()
                                    packet = av.packet.Packet(bytes(obj['data']))
                                    frames = codec.decode(packet)
                                    
                                    if frames:
                                        frame = frames[0]
                                        img = frame.to_ndarray(format='rgb24')
                                        img = img[::-1] # 翻转图像
                                        
                                        with self.image_lock:
                                            self.latest_frame = img
                                        
                                        self.perf_stats['decode'].append(time.time() - t_start)
                                    
                                    del self.frame_buffers[fid]
                                    
                                except Exception:
                                    pass
            except: pass

    def heartbeat_daemon(self):
        while self.running and self.connected:
            try:
                self.udp_socket.sendto(b"HEARTBEAT", self.target_addr)
                time.sleep(1.0)
            except: break

    # ==================== Pyglet 渲染逻辑 ====================

    def update_frame(self, dt):
        if not self.connected: return
        
        with self.image_lock:
            if self.latest_frame is not None:
                frame_data = self.latest_frame
                self.latest_frame = None
            else:
                return
        
        h, w, _ = frame_data.shape
        
        img_data = pyglet.image.ImageData(w, h, 'RGB', frame_data.tobytes(), pitch=w*3)
        
        if self.texture is None or self.texture.width != w or self.texture.height != h:
            self.texture = img_data.get_texture() 
            self.sprite = pyglet.sprite.Sprite(self.texture, x=0, y=0)
            self.sprite.image.anchor_x = 0
            
        else:
            self.texture.blit_into(img_data, 0, 0, 0)
            
        self.perf_stats['render'].append(time.time())
        self.resize_sprite()

    def resize_sprite(self):
        if not self.sprite or not self.texture: return
        win_w, win_h = self.window.width, self.window.height
        tex_w, tex_h = self.texture.width, self.texture.height
        
        ratio = min(win_w / tex_w, win_h / tex_h)
        
        self.sprite.scale = ratio
        
        pos_x = (win_w - tex_w * ratio) / 2
        pos_y = (win_h - tex_h * ratio) / 2
        
        self.sprite.position = (pos_x, pos_y, 0)

    def switch_to_desktop_ui(self):
        res_key = self.resolution_presets[self.current_res_idx]
        w, h = self.resolution_map[res_key]
        self.window.set_size(w, h)
        self.window.set_caption(f"Remote Desktop - {res_key} | F11:切换分辨率 F12:全屏")
        self.fps_display = FPSDisplay(self.window)

    def change_resolution(self, res_key):
        w, h = self.resolution_map[res_key]
        self.window.set_size(w, h)
        self.window.set_caption(f"Remote Desktop - {res_key} | F11:切换分辨率 F12:全屏")
        print(f"[设置] 分辨率切换: {res_key}")
        if self.connected and self.target_addr:
            cmd = json.dumps({'type': 'resolution', 'width': w, 'height': h})
            self.udp_socket.sendto(cmd.encode(), self.target_addr)

    def update_stats(self, dt):
        if not self.connected: return
        if len(self.perf_stats['decode']) > 0:
            avg_dec = sum(self.perf_stats['decode']) / len(self.perf_stats['decode']) * 1000
            print(f"[性能] Decode: {avg_dec:.1f}ms")
        self.perf_stats = {'decode': [], 'render': []}

    def update_status(self, text):
        self.status_label.text = text

    def run(self):
        pyglet.app.run()
        self.running = False

if __name__ == "__main__":
    app = P2PControllerApp()
    app.run()