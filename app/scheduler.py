# app/scheduler.py
import threading
import time
from datetime import datetime, timezone, timedelta
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.tournament_service import TournamentService

class TournamentScheduler:
    """比赛定时调度器"""
    
    def __init__(self, check_interval=60):
        """
        初始化调度器
        :param check_interval: 检查间隔（秒），默认60秒
        """
        self.check_interval = check_interval
        self.running = False
        self.thread = None
    
    def start(self):
        """启动调度器"""
        if self.running:
            return
        
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        print(f"⏰ 定时调度器已启动，检查间隔: {self.check_interval}秒")
    
    def stop(self):
        """停止调度器"""
        self.running = False
        if self.thread:
            self.thread.join()
        print("⏰ 定时调度器已停止")
    
    def _run(self):
        """调度器主循环"""
        while self.running:
            try:
                self._check_tournaments()
            except Exception as e:
                print(f"❌ 调度器执行出错: {e}")
                import traceback
                traceback.print_exc()
            
            time.sleep(self.check_interval)
    
    def _check_tournaments(self):
        """检查所有比赛状态"""
        from app.models.tournament import SessionLocal, TournamentMatch, TournamentConfig
        
        now = datetime.utcnow()
        session = SessionLocal()
        
        try:
            # 1. 检查是否有比赛需要启动（从pending变为running）
            pending_matches = session.query(TournamentMatch).filter(
                TournamentMatch.status == 'pending',
                TournamentMatch.start_time <= now
            ).all()
            
            if pending_matches:
                tournament_ids = set()
                for match in pending_matches:
                    match.status = 'running'
                    tournament_ids.add(match.tournament_id)
                    print(f"🚀 [{now.strftime('%Y-%m-%d %H:%M:%S')}] 比赛 {match.match_id} 已开始")
                
                # 更新比赛状态为running
                for tid in tournament_ids:
                    tournament = session.query(TournamentConfig).get(tid)
                    if tournament and tournament.status == 'pending':
                        tournament.status = 'running'
                        print(f"🚀 比赛 {tournament.name} 已开始")
                
                session.commit()
            
            # 2. 检查并结束超时比赛
            finished_count = TournamentService.check_and_finish_matches()
            if finished_count > 0:
                print(f"✅ [{now.strftime('%Y-%m-%d %H:%M:%S')}] 自动结束了 {finished_count} 场比赛")
            
            # 3. 更新进行中比赛的营收数据（只在比赛已经running时才更新）
            running_matches = session.query(TournamentMatch).filter_by(status='running').all()
            for match in running_matches:
                try:
                    TournamentService.update_match_revenue(match.match_id)
                except Exception as e:
                    print(f"⚠️ 更新比赛 {match.match_id} 营收失败: {e}")
                    
        finally:
            session.close()

# 全局调度器实例
_scheduler = None

def get_scheduler():
    """获取调度器单例"""
    global _scheduler
    if _scheduler is None:
        _scheduler = TournamentScheduler(check_interval=60)  # 每60秒检查一次
    return _scheduler

def start_scheduler():
    """启动调度器"""
    scheduler = get_scheduler()
    scheduler.start()
    return scheduler

def stop_scheduler():
    """停止调度器"""
    global _scheduler
    if _scheduler:
        _scheduler.stop()
        _scheduler = None