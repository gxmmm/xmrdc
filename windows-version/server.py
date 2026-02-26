import socket
import threading
import json

HOST = '0.0.0.0'
PORT = 5667 # 信令端口，不要占用 FRP 的端口

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.bind((HOST, PORT))
server.listen(5)
print(f"[*] 信令服务器监听 {PORT}...")

peers = {} # { code: { 'conn': socket, 'addr': (ip, port) } }

def handle_client(conn, addr):
    try:
        data = conn.recv(1024).decode()
        msg = json.loads(data)
        
        # 1. 被控端注册
        if msg['type'] == 'register':
            code = msg['code']
            peers[code] = {'conn': conn, 'addr': addr, 'udp_port': msg.get('udp_port', 10000)}
            print(f"被控端注册: {code} from {addr}")
            # 保持连接
            while True:
                try:
                    if conn.recv(1024): pass 
                    else: break
                except: break

        # 2. 控制端请求连接
        elif msg['type'] == 'connect':
            code = msg['code']
            if code in peers:
                target = peers[code]
                # 告诉控制端被控端的公网IP和UDP端口
                # 注意：target['addr'][0] 是被控端的TCP公网IP，通常 UDP 公网 IP 也是这个
                # UDP 端口我们假设被控端固定为 10000，或者让被控端在注册包里告知
                resp = {
                    'status': 'found',
                    'peer_ip': target['addr'][0],
                    'peer_port': target['udp_port']
                }
                conn.send(json.dumps(resp).encode())
                
                # 同时通知被控端：有人要连你，请开始向控制端打洞
                # 控制端的公网 UDP 信息我们暂时不知道，只能让被控端“被动”接收
                # 实际上，控制端收到 resp 后会主动发包，被控端收到包后获取了控制端地址
                # 所以这一步可以省略，或者发一个通知让被控端准备好
                
            else:
                conn.send(json.dumps({'status': 'not_found'}).encode())
                
    except Exception as e:
        print(f"Error: {e}")
    finally:
        # 清理断开的连接
        # 简单处理：如果是被控端断开，移除记录
        for k, v in list(peers.items()):
            if v['conn'] == conn:
                del peers[k]
                print(f"移除断开连接: {k}")
        conn.close()

while True:
    conn, addr = server.accept()
    threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()