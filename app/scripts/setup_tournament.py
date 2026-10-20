# app/scripts/setup_tournament.py
import requests
import json

API_BASE = "http://localhost:5000/api/tournament"
ADMIN_TOKEN = "my_secret_password_123"

headers = {
    "X-Admin-Token": ADMIN_TOKEN,
    "Content-Type": "application/json"
}

# 16位选手信息（添加头像扩展名）
players = [
    {"id": "A", "name": "Ameki", "room_id": "27628019", "seed": 197, "avatar_ext": "webp"},
    {"id": "B", "name": "chu2u", "room_id": "1727074031", "seed": 287, "avatar_ext": "webp"},
    {"id": "C", "name": "Nagisa", "room_id": "31368705", "seed": 217, "avatar_ext": "webp"},
    {"id": "D", "name": "Rhea", "room_id": "23260993", "seed": 147, "avatar_ext": "jpg"},
    {"id": "E", "name": "Mizuki", "room_id": "30655190", "seed": 207, "avatar_ext": "webp"},
    {"id": "F", "name": "Hazel", "room_id": "1713546334", "seed": 277, "avatar_ext": "webp"},
    {"id": "G", "name": "Liko", "room_id": "1713548468", "seed": 277, "avatar_ext": "webp"},
    {"id": "H", "name": "Izayoi", "room_id": "1741667419", "seed": 277, "avatar_ext": "webp"},
    {"id": "I", "name": "SUI", "room_id": "25788785", "seed": 177, "avatar_ext": "webp"},
    {"id": "J", "name": "Harei", "room_id": "1820703922", "seed": 237, "avatar_ext": "webp"},
    {"id": "K", "name": "Viridis", "room_id": "1727071052", "seed": 287, "avatar_ext": "webp"},
    {"id": "L", "name": "Sumire", "room_id": "1727076670", "seed": 287, "avatar_ext": "webp"},
    {"id": "M", "name": "Kloa", "room_id": "1986461465", "seed": 277, "avatar_ext": "webp"},
    {"id": "N", "name": "Melody", "room_id": "1947277414", "seed": 247, "avatar_ext": "jpg"},
    {"id": "O", "name": "Mit3uri", "room_id": "1967216004", "seed": 257, "avatar_ext": "webp"},
    {"id": "P", "name": "Shiori", "room_id": "26966466", "seed": 187, "avatar_ext": "webp"},
]

data = {
    "name": "VR斗虫大赛 2026",
    "players": players
}

print("📝 初始化比赛...")
print("正在连接服务器...")

try:
    resp = requests.post(f"{API_BASE}/admin/init", json=data, headers=headers, timeout=10)
    
    if resp.status_code == 200:
        result = resp.json()
        tournament_id = result['tournament_id']
        print(f"\n✅ 比赛初始化成功！")
        print(f"   比赛ID: {tournament_id}")
        print(f"   参赛人数: {len(players)} 人")
        
        with open('tournament_id.txt', 'w') as f:
            f.write(str(tournament_id))
        
        print("\n" + "="*50)
        print("✅ 设置完成！下一步运行:")
        print("   python app\\scripts\\start_round1.py")
        print("="*50)
    else:
        print(f"\n❌ 初始化失败 (状态码 {resp.status_code})")
        print(f"错误信息: {resp.text}")
        
except requests.exceptions.ConnectionError:
    print("\n❌ 无法连接到服务器！")
    print("请确认服务器是否已启动:")
    print("   python run.py")
    
except Exception as e:
    print(f"\n❌ 发生错误: {e}")