# scripts/start_tournament.py
import requests

API_BASE = "http://localhost:5000/api/tournament"
ADMIN_TOKEN = "my_secret_password_123"

headers = {"X-Admin-Token": ADMIN_TOKEN}

try:
    with open('tournament_id.txt', 'r') as f:
        tournament_id = int(f.read().strip())
except:
    print("❌ 找不到 tournament_id.txt，请先运行 setup_tournament.py")
    exit(1)

print(f"🎮 准备开始比赛 (ID: {tournament_id})")
print("系统将自动随机配对第一轮")
input("确认后按回车键开始...")

resp = requests.post(f"{API_BASE}/admin/start-round1/{tournament_id}", headers=headers)

if resp.status_code == 200:
    result = resp.json()
    print("✅ 比赛已开始！")
    print(f"第 {result['round']} 轮已自动配对")
    print("⏰ 3天后自动结束，并自动开始下一轮")
    print("🤖 后续所有轮次将自动配对和推进")
else:
    print(f"❌ 启动失败: {resp.text}")