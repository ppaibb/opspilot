import logging
from apscheduler.schedulers.blocking import BlockingScheduler
from app.db.database import SessionLocal
from app.services.alert_engine.engine import evaluate_once

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def job():
    db = SessionLocal()
    try:
        stats = evaluate_once(db)
        logger.info(f"影子引擎执行统计: {stats}")
    except Exception as exc:
        logger.error(f"影子引擎发生未捕获异常: {exc}", exc_info=True)
    finally:
        db.close()

if __name__ == "__main__":
    logger.info("启动独立影子告警引擎...")
    scheduler = BlockingScheduler()
    scheduler.add_job(job, 'interval', seconds=30)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("独立影子告警引擎已退出。")
