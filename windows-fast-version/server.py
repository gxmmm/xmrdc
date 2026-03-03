import socket
import threading
import json
import time

HOST = '0.0.0.0'
PORT = 5667 # 信令端口，不要占用 FRP 的端口
TIMEOUT = 600  # 超时时间(秒)，若超过此时间未收到心跳或数据，则断开

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.bind((HOST, PORT))
server.listen(5)
print(f"[*] 信令服务器监听 {PORT}...")

peers = {} # { code: { 'conn': socket, 'addr': (ip, port) } }

def handle_client(conn, addr):
    print(f"[*] 新连接: {addr}")
    try:
        # 设置初始超时，防止恶意连接不发数据
        conn.settimeout(TIMEOUT)
        data = conn.recv(1024).decode()
        if not data:
            return
            
        msg = json.loads(data)
        
        # 1. 被控端注册
        if msg['type'] == 'register':
            code = msg['code']
            peers[code] = {'conn': conn, 'addr': addr, 'udp_port': msg.get('udp_port', 10000)}
            print(f"被控端注册: {code} from {addr}")
            
            # 保持连接，循环检测心跳
            while True:
                try:
                    # 接收数据，可能是心跳包或其他控制包
                    res = conn.recv(1024)
                    if not res: 
                        break # 连接关闭
                    
                    # 如果收到数据，解析判断是否为心跳
                    try:
                        recv_msg = json.loads(res.decode())
                        if recv_msg.get('type') == 'heartbeat':
                            # 收到心跳，什么也不做，继续循环等待下一次
                            # print(f"收到心跳: {code}")
                            continue
                        # 其他类型的包可以在这里处理
                    except json.JSONDecodeError:
                        pass # 忽略非JSON数据

                except socket.timeout:
                    # 超时未收到数据，断开连接
                    print(f"心跳超时，断开: {code}")
                    break
                except Exception as e:
                    print(f"连接异常: {e}")
                    break

        # 2. 控制端请求连接
        elif msg['type'] == 'connect':
            code = msg['code']
            if code in peers:
                target = peers[code]
                # 告诉控制端被控端的公网IP和UDP端口
                resp = {
                    'status': 'found',
                    'peer_ip': target['addr'][0],
                    'peer_port': target['udp_port']
                }
                conn.send(json.dumps(resp).encode())
                # 控制端逻辑通常到此完成交互，或者保持连接等待后续指令
                # 这里保持原有逻辑，简单处理
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