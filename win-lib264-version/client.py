import socket
import threading
import json
import time
import struct
import av
import platform
import os
import ctypes
from ctypes import wintypes

import pyglet
from pyglet import gl
from pyglet.window import key, mouse
from pyglet.graphics import shader

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
TARGET_SERVER_IP = 'frp-hat.com'  # FRP地址
TCP_PORT = 43972                  # FRP映射的公网端口
# ===========================================

MTU_SIZE = 1400

FRAME_TYPE_HEADER = 0x4A4B4C4D
FRAME_TYPE_DATA   = 0x4A4B4C4E
I_FRAME_THRESHOLD = 30 * 1024

# ================= UI 样式配置 =================
COLOR_BG = (25, 25, 25, 255)
COLOR_PANEL = (45, 45, 45, 255)
COLOR_PRIMARY = (76, 175, 80, 255)
COLOR_TEXT_WHITE = (255, 255, 255, 255)
COLOR_TEXT_GRAY = (180, 180, 180, 255)
COLOR_TEXT_HINT = (100, 100, 100, 255)
COLOR_ACCENT = (0, 120, 215, 255)
COLOR_BORDER_DEFAULT = (60, 60, 60, 255)
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
        self.input_active = False
        
        # === GPU 资源 ===
        self.y_tex = None
        self.u_tex = None
        self.v_tex = None
        self.shader_program = None
        self.quad_vlist = None
        
        # 初始化 Shader
        self.init_shader()
        
        self.init_login_ui()
        
        self.user32 = ctypes.windll.user32
        
        # === 分辨率预设与状态 ===
        self.resolution_presets = ['480p', '720p', '1080p']
        self.resolution_map = {'480p': (640, 480), '720p': (1280, 720), '1080p': (1920, 1080)}
        self.current_res_idx = 1
        
        self.pre_fullscreen_size = (1280, 720)
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
        pyglet.clock.schedule_interval(self.blink_cursor, 0.5)

        @self.window.event
        def on_activate():
            if self.connected:
                self.clip_cursor_to_sprite()

        @self.window.event
        def on_deactivate():
            self.release_cursor()

    def init_shader(self):
        """初始化 YUV 转 RGB 的 Shader 程序"""
        vertex_shader_source = """
        #version 150 core
        in vec2 position;
        in vec2 texcoord;
        out vec2 v_texcoord;
        void main()
        {
            gl_Position = vec4(position, 0.0, 1.0);
            v_texcoord = texcoord;
        }
        """

        fragment_shader_source = """
        #version 150 core
        uniform sampler2D tex_y;
        uniform sampler2D tex_u;
        uniform sampler2D tex_v;
        in vec2 v_texcoord;
        out vec4 out_color;
        void main()
        {
            float y = texture(tex_y, v_texcoord).r;
            float u = texture(tex_u, v_texcoord).r - 0.5;
            float v = texture(tex_v, v_texcoord).r - 0.5;
            float r = y + 1.402 * v;
            float g = y - 0.344 * u - 0.714 * v;
            float b = y + 1.772 * u;
            out_color = vec4(r, g, b, 1.0);
        }
        """

        try:
            vs = shader.Shader(vertex_shader_source, 'vertex')
            fs = shader.Shader(fragment_shader_source, 'fragment')
            self.shader_program = shader.ShaderProgram(vs, fs)
            
            # 【修复1】某些显卡黑屏问题：设置像素对齐方式为 1
            # 防止 YUV stride 不对齐导致的纹理错位
            gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
        except Exception as e:
            print(f"Shader 初始化失败: {e}")
            raise

    def init_login_ui(self):
        """初始化深色风格登录界面"""
        win_w, win_h = self.window.width, self.window.height
        
        self.label_title = pyglet.text.Label("远端口令", font_name='Microsoft YaHei', font_size=24, 
                                             color=COLOR_TEXT_WHITE,
                                             x=win_w//2, y=win_h - 80, 
                                             anchor_x='center', batch=self.batch)

        self.input_width = 220
        self.input_height = 50
        self.input_x = (win_w - self.input_width) // 2
        self.input_y = win_h - 210
        
        self.input_bg = pyglet.shapes.Rectangle(self.input_x, self.input_y, self.input_width, self.input_height, 
                                                color=COLOR_PANEL[:3], batch=self.batch)
        
        self.input_border = pyglet.shapes.BorderedRectangle(
            self.input_x, self.input_y, self.input_width, self.input_height, 
            border=2, color=COLOR_PANEL[:3], border_color=COLOR_BORDER_DEFAULT[:3], batch=self.batch)

        self.text_input = ""
        self.label_input = pyglet.text.Label("", font_name='Consolas', font_size=28, 
                                             color=COLOR_TEXT_WHITE,
                                             x=win_w//2, y=self.input_y + self.input_height//2, 
                                             anchor_x='center', anchor_y='center', batch=self.batch)
        
        self.label_placeholder = pyglet.text.Label("请输入口令", font_name='Microsoft YaHei', font_size=16, 
                                                   color=COLOR_TEXT_HINT,
                                                   x=win_w//2, y=self.input_y + self.input_height//2, 
                                                   anchor_x='center', anchor_y='center', batch=self.batch)

        self.cursor = pyglet.shapes.Rectangle(0, 0, 2, 30, color=COLOR_TEXT_WHITE, batch=self.batch)
        self.cursor.visible = False

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
        if self.input_active and not self.connected:
            self.cursor.visible = not self.cursor.visible
        else:
            self.cursor.visible = False

    def update_input_style(self):
        if self.input_active:
            self.input_border.border_color = COLOR_ACCENT[:3]
            self.cursor.visible = True
        else:
            self.input_border.border_color = COLOR_BORDER_DEFAULT[:3]
            self.cursor.visible = False
        
        text_width = self.label_input.content_width
        cursor_x = self.window.width//2 + text_width//2 + 4
        cursor_y = self.input_y + (self.input_height - 30) // 2
        self.cursor.position = (cursor_x, cursor_y)

    def release_cursor(self):
        self.user32.ClipCursor(None)

    def clip_cursor_to_sprite(self):
        if not self.y_tex: return
        win_w, win_h = self.window.width, self.window.height
        tex_w, tex_h = self.y_tex.width, self.y_tex.height
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
            if not self.input_active:
                self.input_active = True
                self.update_input_style()

            if symbol == key.BACKSPACE:
                self.text_input = self.text_input[:-1]
            elif symbol == key.ENTER:
                self.start_p2p()
            elif symbol == key.TAB:
                pass
            elif len(self.text_input) < 6:
                val = None
                if key._0 <= symbol <= key._9:
                    val = symbol - key._0
                elif key.NUM_0 <= symbol <= key.NUM_9:
                    val = symbol - key.NUM_0
                
                if val is not None:
                    self.text_input += str(val)
            
            self.label_input.text = self.text_input
            self.label_placeholder.visible = (len(self.text_input) == 0)
            self.update_input_style()
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
        if not self.connected or not self.target_addr or not self.y_tex: return
        vid_w, vid_h = self.y_tex.width, self.y_tex.height
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
            if self.input_x <= x <= self.input_x + self.input_width and \
               self.input_y <= y <= self.input_y + self.input_height:
                self.input_active = True
            else:
                self.input_active = False
            
            self.update_input_style()

            if button == mouse.LEFT:
                if self.btn_connect_rect.x <= x <= self.btn_connect_rect.x + self.btn_connect_rect.width and \
                   self.btn_connect_rect.y <= y <= self.btn_connect_rect.y + self.btn_connect_rect.height:
                    self.start_p2p()
            return

        if self.target_addr and self.y_tex:
            vid_w, vid_h = self.y_tex.width, self.y_tex.height
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
        if self.target_addr and self.y_tex:
            vid_w, vid_h = self.y_tex.width, self.y_tex.height
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
        pyglet.gl.glClearColor(*[c/255.0 for c in COLOR_BG])
        self.window.clear()
        
        if self.connected and self.y_tex:
            self.shader_program.use()
            
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self.y_tex.id)
            self.shader_program['tex_y'] = 0
            
            gl.glActiveTexture(gl.GL_TEXTURE1)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self.u_tex.id)
            self.shader_program['tex_u'] = 1
            
            gl.glActiveTexture(gl.GL_TEXTURE2)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self.v_tex.id)
            self.shader_program['tex_v'] = 2
            
            self.update_quad_vertices()
            self.quad_vlist.draw(gl.GL_TRIANGLE_FAN)
            
            self.shader_program.stop()
            
            if self.fps_display:
                self.fps_display.draw()
        else:
            self.batch.draw()

    def update_quad_vertices(self):
        if not self.y_tex: return
        
        win_w, win_h = self.window.width, self.window.height
        vid_w, vid_h = self.y_tex.width, self.y_tex.height
        
        ratio = min(win_w / vid_w, win_h / vid_h)
        display_w = vid_w * ratio
        display_h = vid_h * ratio
        
        x1 = - (display_w / win_w)
        x2 =   (display_w / win_w)
        y1 = - (display_h / win_h)
        y2 =   (display_h / win_h)
        
        self.quad_vlist.position[:] = [x1, y1, x2, y1, x2, y2, x1, y2]

    # ==================== 连接逻辑 ====================

    def start_p2p(self):
        if not self.text_input: return
        code = self.text_input
        self.status_label.text = "正在连接..."
        self.btn_connect_label.text = "连接中..."
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
            self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8*1024*1024)
            self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8*1024*1024)
            self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.udp_socket.bind(('0.0.0.0', 0))
            
            local_port = self.udp_socket.getsockname()[1]
            local_ip = self.get_local_ip()

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
                peer_public_ip = target_ip
                peer_public_port = resp['peer_public_port']
                peer_local_ip = resp.get('peer_local_ip')
                peer_local_port = resp.get('peer_local_port')
                
                pyglet.clock.schedule_once(lambda dt: self.update_status("并发打洞中..."), 0)
                
                threading.Thread(target=self.recv_loop, daemon=True).start()
                
                threading.Thread(target=self.punch_thread, 
                                 args=((peer_public_ip, peer_public_port),), daemon=True).start()
                
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
                
                if not self.connected:
                    self.connected = True
                    self.target_addr = addr
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
                                    codec.options = {
                                        "threads": "auto",
                                        "flags2": "fast"
                                    }
                                
                                try:
                                    packet = av.packet.Packet(bytes(obj['data']))
                                    frames = codec.decode(packet)
                                    
                                    if frames:
                                        frame = frames[0]
                                        if frame.format.name != 'yuv420p':
                                            frame = frame.reformat(format='yuv420p')
                                        
                                        with self.image_lock:
                                            self.latest_frame = frame
                                    
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
            if self.latest_frame is None:
                return
            frame = self.latest_frame
            self.latest_frame = None
        
        y_plane = frame.planes[0]
        u_plane = frame.planes[1]
        v_plane = frame.planes[2]
        
        w = frame.width
        h = frame.height
        
        y_ptr = ctypes.c_void_p(y_plane.buffer_ptr)
        u_ptr = ctypes.c_void_p(u_plane.buffer_ptr)
        v_ptr = ctypes.c_void_p(v_plane.buffer_ptr)
        
        # 如果纹理不存在或尺寸变化，创建新纹理
        if self.y_tex is None or self.y_tex.width != w or self.y_tex.height != h:
            self.y_tex = pyglet.image.Texture.create(w, h, internalformat=gl.GL_RED)
            self.u_tex = pyglet.image.Texture.create(w//2, h//2, internalformat=gl.GL_RED)
            self.v_tex = pyglet.image.Texture.create(w//2, h//2, internalformat=gl.GL_RED)
            
            # 【修复2】设置纹理过滤模式为 GL_NEAREST
            # 防止远程桌面文字模糊，提高清晰度
            for tex in (self.y_tex, self.u_tex, self.v_tex):
                gl.glBindTexture(gl.GL_TEXTURE_2D, tex.id)
                gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
                gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
            
            if self.quad_vlist is None:
                # 【修复画面倒置】
                # OpenGL 纹理原点在左下角 (0,0)，PyAV/图像数据原点在左上角。
                # 通过翻转纹理坐标 Y 轴 (1-y) 来修正倒置。
                # 原始 Y: 0, 0, 1, 1 -> 翻转后: 1, 1, 0, 0
                # 顺序：左下(0,1), 右下(1,1), 右上(1,0), 左上(0,0)
                self.quad_vlist = self.shader_program.vertex_list(
                    4, gl.GL_TRIANGLE_FAN,
                    position=('f', [-1.0, -1.0, 1.0, -1.0, 1.0, 1.0, -1.0, 1.0]),
                    texcoord=('f', [0.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0]) 
                )

        # 【修复3】使用 UNPACK_ROW_LENGTH 优化 CPU 占用
        # 直接告诉 GPU 数据的步长，避免 CPU 预处理数据拷贝

        # 上传 Y Plane
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.y_tex.id)
        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, y_plane.line_size)
        gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, w, h, gl.GL_RED, gl.GL_UNSIGNED_BYTE, y_ptr)
        
        # 上传 U Plane
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.u_tex.id)
        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, u_plane.line_size)
        gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, w//2, h//2, gl.GL_RED, gl.GL_UNSIGNED_BYTE, u_ptr)

        # 上传 V Plane
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.v_tex.id)
        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, v_plane.line_size)
        gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, w//2, h//2, gl.GL_RED, gl.GL_UNSIGNED_BYTE, v_ptr)
        
        # 恢复状态，避免影响其他绘制
        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, 0)

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

    def update_status(self, text):
        self.status_label.text = text

    def run(self):
        pyglet.app.run()
        self.running = False

if __name__ == "__main__":
    app = P2PControllerApp()
    app.run()