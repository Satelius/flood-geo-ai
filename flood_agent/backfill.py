import threading

from .clients import ServiceError, fetch_flood


class BackfillManager:
    def __init__(self, store, fetcher=None):
        self.store = store
        self.fetcher = fetcher or fetch_flood
        self.lock = threading.RLock()
        self.workers = {}

    def start(self, start_date=None, end_date=None, budget=None, batch_id=None, retry_unavailable=False):
        with self.lock:
            if any(worker['thread'].is_alive() for worker in self.workers.values()):
                raise ValueError('已有历史回填任务正在运行')
            if budget is not None and (not isinstance(budget, int) or isinstance(budget, bool) or budget < 1):
                raise ValueError('budget 必须是正整数')
            if batch_id:
                if start_date or end_date:
                    raise ValueError('续跑批次时不应同时指定日期范围')
                batch = self.store.get_history_batch(batch_id)
                if batch is None:
                    raise ValueError('找不到历史回填批次')
                if budget is None:
                    budget = batch['budget']
            else:
                if not start_date or not end_date:
                    raise ValueError('新建批次需要 start 和 end')
                batch_id = self.store.create_history_batch(start_date, end_date, budget)
            self.store.recover_running_days(batch_id)
            stop = threading.Event()
            thread = threading.Thread(target=self._run, args=(batch_id, budget, retry_unavailable, stop), daemon=True)
            self.workers[batch_id] = {'thread': thread, 'stop': stop}
            self.store.set_history_batch_status(batch_id, 'running')
            thread.start()
            return self.store.get_history_batch(batch_id)

    def _run(self, batch_id, budget, retry_unavailable, stop):
        attempted = 0
        try:
            tasks = self.store.list_history_batch_days(batch_id, retry_unavailable=retry_unavailable)
            for task in tasks:
                if stop.is_set():
                    self.store.set_history_batch_status(batch_id, 'paused')
                    return
                if budget is not None and attempted >= budget:
                    self.store.set_history_batch_status(batch_id, 'budget_exhausted')
                    return
                day = task['date']
                self.store.start_coverage_day(batch_id, day)
                try:
                    result = self.fetcher(self.store, 'archive', day)
                    state = 'complete_nonzero' if result['message_count'] else 'complete_zero'
                    self.store.finish_coverage_day(batch_id, day, result, status=state)
                except ServiceError as error:
                    if error.incomplete:
                        state = 'incomplete'
                    elif error.status_code == 404:
                        state = 'unavailable'
                    else:
                        state = 'failed'
                    self.store.finish_coverage_day(batch_id, day, error.progress, status=state, error=str(error))
                except Exception:
                    self.store.finish_coverage_day(batch_id, day, {}, status='failed', error='采集处理失败；原始快照和运行日志可供核查')
                attempted += 1
            if stop.is_set():
                status = 'paused'
            else:
                batch = self.store.get_history_batch(batch_id)
                complete_days = sum(batch['day_status_counts'].get(state, 0)
                                    for state in ('complete_zero', 'complete_nonzero'))
                status = 'completed' if complete_days == batch['total_days'] else 'completed_with_gaps'
            self.store.set_history_batch_status(batch_id, status)
        except Exception:
            self.store.set_history_batch_status(batch_id, 'failed', '批次调度失败；可重新启动此批次继续')

    def pause(self, batch_id):
        with self.lock:
            worker = self.workers.get(batch_id)
            if not worker or not worker['thread'].is_alive():
                raise ValueError('该批次当前没有运行中的任务')
            worker['stop'].set()
            self.store.set_history_batch_status(batch_id, 'pause_requested')
            return self.store.get_history_batch(batch_id)

    def status(self, batch_id):
        batch = self.store.get_history_batch(batch_id)
        if batch is None:
            return None
        worker = self.workers.get(batch_id)
        batch['worker_running'] = bool(worker and worker['thread'].is_alive())
        return batch
