"""
PyQt6 QThread + QObject 워커 패턴 기반의 비동기 백그라운드 처리 모듈.
모든 무거운 파일 작업, AI OCR, 엑셀 병합, 실시간 폴더/시트 감시 작업은
이 모듈의 BaseWorker 또는 TaskRunner를 통해 실행되어 UI 프리징을 방지합니다.
"""

import time
import traceback
from typing import Any, Callable, Dict, Optional, Tuple
from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot


class WorkerSignals(QObject):
    """백그라운드 워커가 메인 UI 스레드로 상태를 전달하는 공통 시그널."""

    task_started = pyqtSignal(str)          # 작업 이름
    progress = pyqtSignal(int, str)         # (진행률 0~100, 상세 상태 메시지)
    log = pyqtSignal(str, str)              # (로그 레벨 INFO/WARN/ERROR/SUCCESS, 메시지)
    result = pyqtSignal(object)             # 작업 결과 데이터 객체
    error = pyqtSignal(str)                 # 예외/오류 메시지
    task_finished = pyqtSignal(str)         # 완료된 작업 이름


class BaseWorker(QObject):
    """
    QThread에 moveToThread()로 이동하여 실행되는 기본 QObject 워커 클래스.
    하위 클래스는 execute() 메서드를 오버라이드하여 실제 비즈니스 로직을 구현합니다.
    """

    def __init__(self, task_name: str = "백그라운드 작업"):
        super().__init__()
        self.task_name = task_name
        self.signals = WorkerSignals()
        self._stop_requested = False

    def request_stop(self) -> None:
        """안전한 작업 중단을 요청합니다."""
        self._stop_requested = True

    def is_stop_requested(self) -> bool:
        """작업 중단 요청 여부를 반환합니다."""
        return self._stop_requested

    def emit_progress(self, percent: int, message: str = "") -> None:
        """진행률(0~100)과 상태 텍스트를 메인 윈도우로 전달합니다."""
        clamped = max(0, min(100, int(percent)))
        self.signals.progress.emit(clamped, message)

    def emit_log(self, level: str, message: str) -> None:
        """실시간 로그 콘솔 및 DB에 기록할 메시지를 전달합니다."""
        self.signals.log.emit(level, f"[{self.task_name}] {message}")

    @pyqtSlot()
    def run(self) -> None:
        """QThread.started 시그널에 연결되는 진입 슬롯."""
        self.signals.task_started.emit(self.task_name)
        try:
            res = self.execute()
            if not self._stop_requested:
                self.signals.result.emit(res)
        except Exception as exc:
            tb = traceback.format_exc()
            err_msg = f"{self.task_name} 중 오류 발생: {exc}\n{tb}"
            self.signals.error.emit(err_msg)
            self.emit_log("ERROR", f"오류 발생: {exc}")
        finally:
            self.signals.task_finished.emit(self.task_name)

    def execute(self) -> Any:
        """하위 클래스에서 구현할 실제 작업 메서드."""
        raise NotImplementedError("BaseWorker 하위 클래스는 execute()를 구현해야 합니다.")


class FunctionWorker(BaseWorker):
    """
    별도 서브클래싱 없이 일반 함수를 백그라운드 QThread에서 실행하기 위한 워커.
    대상 함수의 첫 번째 인자로 worker 인스턴스(self)가 전달됩니다.
    """

    def __init__(
        self,
        task_name: str,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ):
        super().__init__(task_name=task_name)
        self._func = func
        self._args = args
        self._kwargs = kwargs

    def execute(self) -> Any:
        return self._func(self, *self._args, **self._kwargs)


class PeriodicWorker(BaseWorker):
    """
    일정 주기(interval_sec)마다 콜백을 반복 실행하는 데몬형 백그라운드 워커.
    폴더 폴링 감시나 구글 시트 신규 응답 감시 등에 사용됩니다.
    """

    def __init__(
        self,
        task_name: str,
        interval_sec: float,
        tick_func: Callable[["PeriodicWorker"], Any],
    ):
        super().__init__(task_name=task_name)
        self.interval_sec = max(0.5, float(interval_sec))
        self._tick_func = tick_func

    def execute(self) -> None:
        self.emit_log("INFO", f"실시간 감시 시작 (주기: {self.interval_sec}초)")
        while not self.is_stop_requested():
            try:
                self._tick_func(self)
            except Exception as exc:
                self.emit_log("ERROR", f"주기 작업 중 예외: {exc}")

            steps = max(1, int(self.interval_sec / 0.2))
            for _ in range(steps):
                if self.is_stop_requested():
                    break
                time.sleep(0.2)
        self.emit_log("INFO", "실시간 감시가 안전하게 종료되었습니다.")


class TaskRunner(QObject):
    """
    활성 QThread와 BaseWorker의 생명주기를 관리하고,
    메인 윈도우의 하단 상태바/로그 콘솔과 자동으로 시그널을 연결하는 매니저.
    """

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._active_tasks: Dict[str, Tuple[QThread, BaseWorker]] = {}
        self._task_counter = 0
        self.global_signals = WorkerSignals()

    def start_worker(
        self,
        worker: BaseWorker,
        task_key: Optional[str] = None,
        on_result: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_finished: Optional[Callable[[], None]] = None,
    ) -> str:
        if task_key is None:
            self._task_counter += 1
            task_key = f"task_{self._task_counter}_{worker.task_name}"

        if task_key in self._active_tasks:
            self.stop_task(task_key)

        thread = QThread()
        worker.moveToThread(thread)

        worker.signals.task_started.connect(self.global_signals.task_started.emit)
        worker.signals.progress.connect(self.global_signals.progress.emit)
        worker.signals.log.connect(self.global_signals.log.emit)
        worker.signals.error.connect(self.global_signals.error.emit)
        worker.signals.task_finished.connect(self.global_signals.task_finished.emit)

        if on_result is not None:
            worker.signals.result.connect(on_result)
        if on_error is not None:
            worker.signals.error.connect(on_error)
        if on_finished is not None:
            worker.signals.task_finished.connect(lambda _: on_finished())

        thread.started.connect(worker.run)
        worker.signals.task_finished.connect(thread.quit)
        worker.signals.task_finished.connect(worker.deleteLater)
        thread.finished.connect(lambda key=task_key: self._cleanup_task(key))
        thread.finished.connect(thread.deleteLater)

        self._active_tasks[task_key] = (thread, worker)
        thread.start()
        return task_key

    def is_running(self, task_key: str) -> bool:
        if task_key not in self._active_tasks:
            return False
        thread, _ = self._active_tasks[task_key]
        return thread.isRunning()

    def stop_task(self, task_key: str, wait_ms: int = 3000) -> None:
        if task_key not in self._active_tasks:
            return
        thread, worker = self._active_tasks[task_key]
        worker.request_stop()
        if thread.isRunning():
            thread.quit()
            # 메인 스레드를 블로킹하는 thread.wait()을 직접 호출하지 않고
            # worker의 stop_checker에 의해 자발적으로 execute() 루프가 끝나도록 유도
        # 태스크 딕셔너리 정리는 thread.finished 시그널에 연결된 _cleanup_task에서 수행

    def stop_all(self, wait_ms: int = 2000) -> None:
        keys = list(self._active_tasks.keys())
        for key in keys:
            self.stop_task(key, wait_ms=wait_ms)

    def _cleanup_task(self, task_key: str) -> None:
        self._active_tasks.pop(task_key, None)
