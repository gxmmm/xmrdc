import socket
import threading
import json
import time
import tkinter as tk
from PIL import Image, ImageTk, ImageDraw
import io
import struct
from queue import Queue
import os

SERVER_IP = 'frp-hat.com' # 修改这里
SERVER_PORT = 43972
MTU_SIZE = 1300

class P2PControllerApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("远程桌面控制端")
        self.root.geometry("400x300")
        
        self.udp_socket = None
        self.target_addr = None
        self.connected = False
        self.running = True
        
        self.frame_buffers = {}
        self.frame_lock = threading.Lock()
        self.render_queue = Queue(maxsize=1) 
        
        # 坐标映射
        self.offset_x = 0
        self.offset_y = 0
        self.scale_x = 1.0
        self.scale_y = 1.0
        
        # 虚拟光标
        self.cursor_pos = (0, 0)
        self.cursor_img = self.load_cursor_image()
        
        # GUI
        self.frame_login = tk.Frame(self.root)
        self.frame_login.pack(pady=40)
        
        tk.Label(self.frame_login, text="远程桌面 P2P", font=("Arial", 16, "bold")).pack(pady=10)
        tk.Label(self.frame_login, text="输入口令:").pack(pady=5)
        self.entry_code = tk.Entry(self.frame_login, width=20, font=("Arial", 12))
        self.entry_code.pack(pady=5)
        self.btn_connect = tk.Button(self.frame_login, text="连接", command=self.start_p2p, width=15, bg="#4CAF50", fg="white")
        self.btn_connect.pack(pady=15)
        
        self.status_label = tk.Label(self.root, text="状态: 未连接", fg="gray")
        self.status_label.pack()
        
        self.canvas = None
        self.top_bar = None
        self.is_fullscreen = False
        
        # 渲染优化变量
        self.canvas_image_obj = None # 画布上的图像对象句柄

    def load_cursor_image(self):
        cursor_path = "cursor.png"
        if os.path.exists(cursor_path):
            try:
                img = Image.open(cursor_path).convert("RGBA")
                img = img.resize((24, 24), Image.LANCZOS)
                return ImageTk.PhotoImage(img)
            except: pass
        
        # 备用内置光标
        img = Image.new('RGBA', (24, 24), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.polygon([(0,0), (0,16), (5,12), (9,22), (13,22), (9,12), (23,12), (23,9), (9,9), (13,0)], fill=(0,0,0,255))
        draw.polygon([(1,1), (1,15), (6,11), (10,21), (12,21), (8,11), (22,11), (22,10), (8,10), (12,1)], fill=(255,255,255,255))
        return ImageTk.PhotoImage(img)

    def start_p2p(self):
        code = self.entry_code.get()
        if not code: return
        self.btn_connect.config(state=tk.DISABLED)
        self.status_label.config(text="查询中...")
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
                peer_ip = msg['peer_ip']
                peer_port = int(msg['peer_port'])
                self.target_addr = (peer_ip, peer_port)
                
                self.update_status(f"打洞 {self.target_addr}...")
                self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2*1024*1024) # 增大接收缓冲
                self.udp_socket.bind(('0.0.0.0', 0))
                threading.Thread(target=self.recv_loop, daemon=True).start()
                
                punch_msg = json.dumps({'type': 'punch'}).encode()
                for _ in range(100):
                    self.udp_socket.sendto(punch_msg, self.target_addr)
                    time.sleep(0.05)
                    if self.connected: break
                if not self.connected:
                    self.update_status("打洞失败")
                    self.btn_connect.config(state=tk.NORMAL)
            else:
                self.update_status("未找到口令")
                self.btn_connect.config(state=tk.NORMAL)
        except Exception as e:
            self.update_status(f"错误: {e}")
            self.btn_connect.config(state=tk.NORMAL)

    def recv_loop(self):
        self.udp_socket.settimeout(15.0)
        last_fid = -1
        while self.running:
            try:
                data, addr = self.udp_socket.recvfrom(65535)
                
                if data == b"PUNCH_OK":
                    self.connected = True
                    self.root.after(0, self.show_desktop_ui)
                    threading.Thread(target=self.heartbeat_daemon, daemon=True).start()
                    continue
                
                if not self.connected: continue
                if len(data) < 20: continue
                
                header = data[:10]
                msg_type = struct.unpack('!10s', header)[0]
                
                if msg_type == b'FRAMEDATA\x00':
                    fid, total_size, chunk_index = struct.unpack('!IIH', data[10:20])
                    chunk_data = data[20:]
                    
                    with self.frame_lock:
                        if fid != last_fid:
                            if last_fid in self.frame_buffers:
                                old_frame = self.frame_buffers.pop(last_fid)
                                if self.render_queue.full(): self.render_queue.get_nowait()
                                self.render_queue.put(bytes(old_frame['data']))
                            self.frame_buffers = {k:v for k,v in self.frame_buffers.items() if k == fid}
                            last_fid = fid
                        
                        if fid not in self.frame_buffers:
                             self.frame_buffers[fid] = {'total_size': total_size, 'data': bytearray(total_size), 'received_map': set()}
                        
                        frame_obj = self.frame_buffers[fid]
                        start_pos = chunk_index * MTU_SIZE
                        if start_pos + len(chunk_data) <= frame_obj['total_size']:
                            frame_obj['data'][start_pos : start_pos + len(chunk_data)] = chunk_data
                            frame_obj['received_map'].add(chunk_index)
                            
                            total_chunks = (frame_obj['total_size'] + MTU_SIZE - 1) // MTU_SIZE
                            if len(frame_obj['received_map']) == total_chunks:
                                if self.render_queue.full(): self.render_queue.get_nowait()
                                self.render_queue.put(bytes(frame_obj['data']))
                                del self.frame_buffers[fid]
                                last_fid = fid + 1
                                        
            except socket.timeout:
                self.update_status("连接超时断开"); break
            except: pass
        self.connected = False

    def heartbeat_daemon(self):
        while self.running and self.connected:
            self.udp_socket.sendto(b"HEARTBEAT", self.target_addr)
            time.sleep(1.0)

    def show_desktop_ui(self):
        self.root.geometry("1280x720")
        self.root.minsize(800, 600)
        
        self.frame_login.pack_forget()
        self.status_label.pack_forget()
        
        self.canvas = tk.Canvas(self.root, bg='black', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        
        # 预创建画布对象，避免重复创建
        self.canvas_image_obj = self.canvas.create_image(0, 0, anchor=tk.NW)
        self.canvas_cursor_obj = self.canvas.create_image(0, 0, anchor=tk.NW, image=self.cursor_img)
        
        self.top_bar = tk.Frame(self.root, bg="#333333", height=30)
        self.top_bar.place(x=0, y=-30, relwidth=1, height=30)
        
        tk.Label(self.top_bar, text="远程桌面已连接 (P2P) | 双击全屏", bg="#333", fg="white", font=("Arial", 10)).pack(side=tk.LEFT, padx=10, pady=5)
        tk.Button(self.top_bar, text="断开连接", command=self.on_close, bg="#d32f2f", fg="white", bd=0, font=("Arial", 10)).pack(side=tk.RIGHT, padx=10, pady=5)
        
        self.canvas.bind("<Motion>", self.on_mouse_move)
        self.canvas.bind("<Button-1>", self.send_mouse_click)
        self.canvas.bind("<Double-Button-1>", self.toggle_fullscreen)
        
        self.root.bind("<Escape>", self.exit_fullscreen)
        self.root.bind("<F11>", lambda e: self.toggle_fullscreen())
        
        self.render_loop()

    def toggle_fullscreen(self, event=None):
        if not self.is_fullscreen:
            self.root.attributes('-fullscreen', True)
            self.root.attributes('-topmost', True)
            self.is_fullscreen = True
        else:
            self.exit_fullscreen()

    def on_mouse_move(self, event):
        self.cursor_pos = (event.x, event.y)
        # 即时更新光标位置 (无需重绘整个画面)
        self.canvas.coords(self.canvas_cursor_obj, event.x, event.y)
        
        local_x = event.x - self.offset_x
        local_y = event.y - self.offset_y
        remote_x = int(local_x / self.scale_x) if self.scale_x != 0 else 0
        remote_y = int(local_y / self.scale_y) if self.scale_y != 0 else 0
        
        if self.connected:
            cmd = json.dumps({'type': 'mouse', 'x': remote_x, 'y': remote_y})
            self.udp_socket.sendto(cmd.encode(), self.target_addr)
            
        screen_h = self.root.winfo_height()
        if event.y < screen_h * 0.05: self.show_top_bar()
        else: self.hide_top_bar()

    def show_top_bar(self): self.top_bar.place(y=0)
    def hide_top_bar(self):
        x, y = self.root.winfo_pointerxy()
        local_y = y - self.root.winfo_rooty()
        if local_y > 30: self.top_bar.place(y=-30)

    def exit_fullscreen(self, event=None):
        if self.is_fullscreen:
            self.root.attributes('-fullscreen', False)
            self.root.attributes('-topmost', False)
            self.is_fullscreen = False

    def render_loop(self):
        if not self.running: return
        try:
            img_bytes = self.render_queue.get_nowait()
            self.display_frame(img_bytes)
        except:
            pass
        self.root.after(10, self.render_loop) # 提高渲染频率

    def display_frame(self, img_bytes):
        try:
            img = Image.open(io.BytesIO(img_bytes))
            
            canvas_w = self.canvas.winfo_width()
            canvas_h = self.canvas.winfo_height()
            if canvas_w < 10: return
            
            img_w, img_h = img.size
            ratio = min(canvas_w / img_w, canvas_h / img_h)
            new_w = int(img_w * ratio)
            new_h = int(img_h * ratio)
            
            self.scale_x = ratio
            self.scale_y = ratio
            self.offset_x = (canvas_w - new_w) // 2
            self.offset_y = (canvas_h - new_h) // 2
            
            if new_w > 0 and new_h > 0:
                img = img.resize((new_w, new_h), Image.BILINEAR)
            
            self.canvas_img = ImageTk.PhotoImage(image=img)
            # 核心优化：直接更新现有对象，而不是删除重建
            self.canvas.itemconfig(self.canvas_image_obj, image=self.canvas_img)
            self.canvas.coords(self.canvas_image_obj, self.offset_x, self.offset_y)
            
            # 确保光标在最上层
            self.canvas.tag_raise(self.canvas_cursor_obj)
            
        except: pass

    def send_mouse_click(self, event):
        local_x = event.x - self.offset_x
        local_y = event.y - self.offset_y
        remote_x = int(local_x / self.scale_x)
        remote_y = int(local_y / self.scale_y)
        
        if self.connected:
            cmd = json.dumps({'type': 'mouse', 'x': remote_x, 'y': remote_y, 'click': True})
            self.udp_socket.sendto(cmd.encode(), self.target_addr)

    def update_status(self, text):
        if self.running: self.status_label.config(text=text)
        
    def run(self): 
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.mainloop()
        
    def on_close(self):
        self.running = False
        self.root.destroy()

if __name__ == "__main__":
    app = P2PControllerApp()
    app.run()