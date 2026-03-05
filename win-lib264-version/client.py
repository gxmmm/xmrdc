import socket
import threading
import json
import time
import struct
import av
import platform
import os
import numpy as np
import pyglet
from pyglet import gl
from pyglet.window import key, mouse
import ctypes
from ctypes import wintypes

# Windows特定优化
if platform.system() == 'Windows':
    try:
        import psutil
        kernel32 = ctypes.windll.kernel32
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), 0x00000003)
        kernel32.SetProcessWorkingSetSize(kernel32.GetCurrentProcess(), -1, -1)
        p = psutil.Process(os.getpid())
        if os.cpu_count() > 1:
            p.cpu_affinity([0, 1])
    except Exception:
        pass

# ================= 配置区域 =================
TARGET_SERVER_IP = 'your-frp.com'  # FRP地址
TCP_PORT = 43972                  # FRP映射的公网端口
# ===========================================

MTU_SIZE = 1400

FRAME_TYPE_HEADER = 0x4A4B4C4D
FRAME_TYPE_DATA   = 0x4A4B4C4E
I_FRAME_THRESHOLD = 30 * 1024

# ================= UI 样式配置 (与被控端一致的深色风格) =================
COLOR_BG = (25, 25, 25, 255)             # 深灰背景 #191919
COLOR_PANEL = (45, 45, 45, 255)          # 输入框/面板背景 #2D2D2D
COLOR_PRIMARY = (76, 175, 80, 255)       # 绿色按钮 (连接) #4CAF50
COLOR_TEXT_WHITE = (255, 255, 255, 255)  # 标题文字
COLOR_TEXT_GRAY = (180, 180, 180, 255)   # 提示文字
COLOR_TEXT_HINT = (100, 100, 100, 255)   # 暗淡提示
COLOR_ACCENT = (0, 120, 215, 255)        # 激活状态蓝色边框 #0078D7
COLOR_BORDER_DEFAULT = (60, 60, 60, 255) # 默认边框颜色
# =====================================================================

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
        self.window = pyglet.window.Window(500, 400, caption="远程桌面", resizable=True)
        self.window.set_vsync(False) 
        
        # === GUI 组件 ===
        self.batch = pyglet.graphics.Batch()
        
        # === 输入状态 ===
        self.input_active = False  # 输入框是否激活
        
        self.init_login_ui()
        
        # === 桌面显示组件 ===
        self.texture = None
        self.sprite = None
        self.user32 = ctypes.windll.user32
        
        # === 分辨率预设与状态 ===
        self.resolution_presets = ['480p', '720p', '1080p']
        self.resolution_map = {'480p': (640, 480), '720p': (1280, 720), '1080p': (1920, 1080)}
        self.current_res_idx = 1
        
        self.pre_fullscreen_size = (1280, 720)
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
        
        pyglet.clock.schedule_interval(self.update_frame, 1/60.0)
        pyglet.clock.schedule_interval(self.update_stats, 2.0)
        pyglet.clock.schedule_interval(self.blink_cursor, 0.5) # 光标闪烁定时器

        @self.window.event
        def on_activate():
            if self.connected:
                self.clip_cursor_to_sprite()

        @self.window.event
        def on_deactivate():
            self.release_cursor()

    def init_login_ui(self):
        """初始化深色风格登录界面"""
        win_w, win_h = self.window.width, self.window.height
        
        # 1. 标题 "远程桌面"
        self.label_title = pyglet.text.Label("远端口令", font_name='Microsoft YaHei', font_size=24, 
                                             color=COLOR_TEXT_WHITE,
                                             x=win_w//2, y=win_h - 80, 
                                             anchor_x='center', batch=self.batch)

        # 2. 远端连接口令 (提示文字)
        # self.label_code_title = pyglet.text.Label("远端连接口令", font_name='Microsoft YaHei', font_size=13, 
        #                                      color=COLOR_TEXT_GRAY,
        #                                      x=win_w//2, y=win_h - 140, 
        #                                      anchor_x='center', batch=self.batch)

        # 3. 输入框背景
        self.input_width = 220
        self.input_height = 50
        self.input_x = (win_w - self.input_width) // 2
        self.input_y = win_h - 210
        
        # 背景
        self.input_bg = pyglet.shapes.Rectangle(self.input_x, self.input_y, self.input_width, self.input_height, 
                                                color=COLOR_PANEL[:3], batch=self.batch)
        
        # 边框 - 默认状态
        self.input_border = pyglet.shapes.BorderedRectangle(
            self.input_x, self.input_y, self.input_width, self.input_height, 
            border=2, color=COLOR_PANEL[:3], border_color=COLOR_BORDER_DEFAULT[:3], batch=self.batch)

        # 4. 输入文字
        self.text_input = ""
        self.label_input = pyglet.text.Label("", font_name='Consolas', font_size=28, 
                                             color=COLOR_TEXT_WHITE,
                                             x=win_w//2, y=self.input_y + self.input_height//2, 
                                             anchor_x='center', anchor_y='center', batch=self.batch)
        
        # 占位符
        self.label_placeholder = pyglet.text.Label("请输入口令", font_name='Microsoft YaHei', font_size=16, 
                                                   color=COLOR_TEXT_HINT,
                                                   x=win_w//2, y=self.input_y + self.input_height//2, 
                                                   anchor_x='center', anchor_y='center', batch=self.batch)

        # 光标
        self.cursor = pyglet.shapes.Rectangle(0, 0, 2, 30, color=COLOR_TEXT_WHITE, batch=self.batch)
        self.cursor.visible = False

        # 5. 连接按钮 (绿色)
        btn_w = 140
        btn_h = 42
        btn_x = (win_w - btn_w) // 2
        btn_y = self.input_y - 70
        
        self.btn_connect_rect = pyglet.shapes.Rectangle(btn_x, btn_y, btn_w, btn_h, 
                                                        color=COLOR_PRIMARY[:3], batch=self.batch)
        self.btn_connect_label = pyglet.text.Label("连接", font_name='Microsoft YaHei', font_size=15, 
                                                   color=COLOR_TEXT_WHITE,
                                                   x=btn_x + btn_w//2, y=btn_y + btn_h//2, 
                                                   anchor_x='center', anchor_y='center', batch=self.batch)

        # 6. 底部信息栏
        self.status_label = pyglet.text.Label("等待连接...", font_name='Microsoft YaHei', font_size=12, 
                                              color=(100, 200, 100, 255), 
                                              x=win_w//2, y=btn_y - 50, 
                                              anchor_x='center', batch=self.batch)
        
        self.ip_label = pyglet.text.Label(f"本机IP: {self.get_local_ip()} | 目标: {TARGET_SERVER_IP}", 
                                          font_name='Microsoft YaHei', font_size=10, 
                                          color=COLOR_TEXT_HINT,
                                          x=win_w//2, y=30, 
                                          anchor_x='center', batch=self.batch)

    def blink_cursor(self, dt):
        """光标闪烁逻辑"""
        if self.input_active and not self.connected:
            self.cursor.visible = not self.cursor.visible
        else:
            self.cursor.visible = False

    def update_input_style(self):
        """更新输入框样式"""
        if self.input_active:
            # 激活状态：蓝色边框
            self.input_border.border_color = COLOR_ACCENT[:3]
            self.cursor.visible = True # 立即显示光标
        else:
            # 非激活状态：灰色边框
            self.input_border.border_color = COLOR_BORDER_DEFAULT[:3]
            self.cursor.visible = False
        
        # 更新光标位置
        text_width = self.label_input.content_width
        # 计算光标X坐标：文本中心 + 文本宽度/2 + 2px间隙
        cursor_x = self.window.width//2 + text_width//2 + 4
        cursor_y = self.input_y + (self.input_height - 30) // 2
        self.cursor.position = (cursor_x, cursor_y)

    def release_cursor(self):
        self.user32.ClipCursor(None)

    def clip_cursor_to_sprite(self):
        if not self.sprite: return
        win_w, win_h = self.window.width, self.window.height
        tex_w, tex_h = self.texture.width, self.texture.height
        ratio = min(win_w / tex_w, win_h / tex_h)
        display_w = tex_w * ratio
        display_h = tex_h * ratio
        offset_x = (win_w - display_w) / 2
        offset_y = (win_h - display_h) / 2
        rect = wintypes.RECT()
        hwnd = self.window._hwnd
        self.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        rect.left += int(offset_x)
        rect.top += int(offset_y)
        rect.right = rect.left + int(display_w)
        rect.bottom = rect.top + int(display_h)
        self.user32.ClipCursor(ctypes.byref(rect)) 
    
    def on_key_press(self, symbol, modifiers):
        if not self.connected:
            # 如果按键按下，自动激活输入框
            if not self.input_active:
                self.input_active = True
                self.update_input_style()

            if symbol == key.BACKSPACE:
                self.text_input = self.text_input[:-1]
            elif symbol == key.ENTER:
                self.start_p2p()
            elif symbol == key.TAB:
                # 可以在此处理Tab切换焦点，暂时忽略
                pass
            # 限制只能输入数字，且长度不超过6位
            elif len(self.text_input) < 6:
                val = None
                if key._0 <= symbol <= key._9:
                    val = symbol - key._0
                elif key.NUM_0 <= symbol <= key.NUM_9:
                    val = symbol - key.NUM_0
                
                if val is not None:
                    self.text_input += str(val)
            
            # 更新UI
            self.label_input.text = self.text_input
            self.label_placeholder.visible = (len(self.text_input) == 0)
            self.update_input_style() # 更新光标位置
            return

        if symbol == key.F11:
            self.current_res_idx = (self.current_res_idx + 1) % len(self.resolution_presets)
            new_res = self.resolution_presets[self.current_res_idx]
            self.change_resolution(new_res)
            return

        if symbol == key.F12:
            self.toggle_fullscreen(not self.window.fullscreen)
            return
            
        if symbol == key.ESCAPE:
            if self.window.fullscreen:
                self.toggle_fullscreen(False)
                return

        try:
            k_name = pyglet.window.key.symbol_string(symbol)
            cmd = json.dumps({'type': 'key', 'key': k_name, 'action': 'press'})
            if self.target_addr: self.udp_socket.sendto(cmd.encode(), self.target_addr)
        except: pass

    def on_key_release(self, symbol, modifiers):
        if not self.connected: return
        try:
            k_name = pyglet.window.key.symbol_string(symbol)
            cmd = json.dumps({'type': 'key', 'key': k_name, 'action': 'release'})
            if self.target_addr: self.udp_socket.sendto(cmd.encode(), self.target_addr)
        except: pass

    def on_mouse_motion(self, x, y, dx, dy):
        if not self.connected or not self.target_addr or not self.texture: return
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
            cmd = json.dumps({'type': 'mouse', 'x': target_x, 'y': target_y})
            self.udp_socket.sendto(cmd.encode(), self.target_addr)

    def on_mouse_drag(self, x, y, dx, dy, buttons, modifiers):
        self.on_mouse_motion(x, y, dx, dy)

    def on_mouse_press(self, x, y, button, modifiers):
        if not self.connected:
            # 检测是否点击了输入框
            if self.input_x <= x <= self.input_x + self.input_width and \
               self.input_y <= y <= self.input_y + self.input_height:
                self.input_active = True
            else:
                self.input_active = False
            
            self.update_input_style()

            # 检测按钮点击
            if button == mouse.LEFT:
                if self.btn_connect_rect.x <= x <= self.btn_connect_rect.x + self.btn_connect_rect.width and \
                   self.btn_connect_rect.y <= y <= self.btn_connect_rect.y + self.btn_connect_rect.height:
                    self.start_p2p()
            return

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

    def toggle_fullscreen(self, enable):
        if enable:
            self.pre_fullscreen_size = (self.window.width, self.window.height)
            self.window.set_fullscreen(True)
        else:
            self.window.set_fullscreen(False)
            w, h = self.pre_fullscreen_size
            self.window.set_size(w, h)

    def on_draw(self):
        # 设置深色背景
        pyglet.gl.glClearColor(*[c/255.0 for c in COLOR_BG])
        self.window.clear()
        
        if self.connected:
            if self.sprite:
                self.sprite.draw()
            if self.fps_display:
                self.fps_display.draw()
        else:
            self.batch.draw()

    # ==================== 连接逻辑 ====================

    def start_p2p(self):
        if not self.text_input: return
        code = self.text_input
        self.status_label.text = "正在连接..."
        self.btn_connect_label.text = "连接中..."
        # 直接使用硬编码的服务器IP
        threading.Thread(target=self.p2p_worker, args=(code, TARGET_SERVER_IP), daemon=True).start()

    def get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"

    def p2p_worker(self, code, target_ip):
        try:
            # 1. 准备 UDP
            self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8*1024*1024)
            self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8*1024*1024)
            self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.udp_socket.bind(('0.0.0.0', 0))
            
            local_port = self.udp_socket.getsockname()[1]
            local_ip = self.get_local_ip()

            # 2. TCP 握手
            tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tcp_sock.settimeout(5.0)
            tcp_sock.connect((target_ip, TCP_PORT))
            
            req = json.dumps({
                'type': 'connect', 
                'code': code, 
                'udp_port': local_port,
                'local_ip': local_ip,
                'local_port': local_port
            })
            tcp_sock.send(req.encode())
            
            resp_data = tcp_sock.recv(1024)
            resp = json.loads(resp_data.decode())
            tcp_sock.close()
            
            if resp.get('status') == 'ok':
                # 3. 解析双地址
                peer_public_ip = target_ip
                peer_public_port = resp['peer_public_port']
                peer_local_ip = resp.get('peer_local_ip')
                peer_local_port = resp.get('peer_local_port')
                
                pyglet.clock.schedule_once(lambda dt: self.update_status("并发打洞中..."), 0)
                
                # 4. 启动接收线程
                threading.Thread(target=self.recv_loop, daemon=True).start()
                
                # 5. 并发打洞 - 公网
                threading.Thread(target=self.punch_thread, 
                                 args=((peer_public_ip, peer_public_port),), daemon=True).start()
                
                # 5. 并发打洞 - 局域网 (如果IP不同)
                if peer_local_ip and peer_local_ip != peer_public_ip:
                    threading.Thread(target=self.punch_thread, 
                                 args=((peer_local_ip, peer_local_port),), daemon=True).start()
            else:
                pyglet.clock.schedule_once(lambda dt: self.update_status("口令错误或拒绝"), 0)
                
        except ConnectionRefusedError:
             pyglet.clock.schedule_once(lambda dt: self.update_status("连接被拒绝(检查端口)"), 0)
        except socket.timeout:
             pyglet.clock.schedule_once(lambda dt: self.update_status("连接超时(检查IP)"), 0)
        except Exception as e:
            pyglet.clock.schedule_once(lambda dt, err=str(e): self.update_status(f"错误: {err}"), 0)

    def punch_thread(self, addr):
        """疯狂发送打洞包"""
        msg = b"PUNCH_SYNC"
        for _ in range(50):
            if self.connected: break
            try:
                self.udp_socket.sendto(msg, addr)
                time.sleep(0.05)
            except: pass

    def recv_loop(self):
        codec = None
        self.udp_socket.settimeout(0.5)
        latest_fid = -1
        
        while self.running:
            try:
                data, addr = self.udp_socket.recvfrom(65535)
                
                # === 自动锁定通道 ===
                if not self.connected:
                    self.connected = True
                    self.target_addr = addr # 锁定最先响应的地址
                    pyglet.clock.schedule_once(lambda dt: self.switch_to_desktop_ui(), 0)
                    threading.Thread(target=self.heartbeat_daemon, daemon=True).start()
                    print(f"[连接成功] 最佳通道: {addr}")
                
                if data == b"PUNCH_OK":
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
                                    codec = av.CodecContext.create('h264', 'r')
                                    codec.thread_type = 'auto'
                                
                                try:
                                    t_start = time.time()
                                    packet = av.packet.Packet(bytes(obj['data']))
                                    frames = codec.decode(packet)
                                    
                                    if frames:
                                        frame = frames[0]
                                        img = frame.to_ndarray(format='rgb24')
                                        img = img[::-1]
                                        
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

    # ==================== 渲染逻辑 ====================

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
        self.window.set_caption(f"已连接 - {res_key} | F11:分辨率 F12:全屏")
        if self.connected and self.target_addr:
            cmd = json.dumps({'type': 'resolution', 'width': w, 'height': h})
            self.udp_socket.sendto(cmd.encode(), self.target_addr)

    def change_resolution(self, res_key):
        w, h = self.resolution_map[res_key]
        self.window.set_size(w, h)
        self.window.set_caption(f"已连接 - {res_key} | F11:分辨率 F12:全屏")
        if self.connected and self.target_addr:
            cmd = json.dumps({'type': 'resolution', 'width': w, 'height': h})
            self.udp_socket.sendto(cmd.encode(), self.target_addr)

    def update_stats(self, dt):
        if not self.connected: return
        self.perf_stats = {'decode': [], 'render': []}

    def update_status(self, text):
        self.status_label.text = text

    def run(self):
        pyglet.app.run()
        self.running = False

if __name__ == "__main__":
    app = P2PControllerApp()
    app.run()