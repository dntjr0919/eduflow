"""
단일 인스턴스 실행 보장 모듈 (core/single_instance.py).
- PyQt6.QtNetwork (QLocalServer / QLocalSocket)를 사용하여 중복 실행을 방지합니다.
- 이미 실행 중인 인스턴스가 있으면 "SHOW" 메시지를 전송하고 새 프로세스를 즉시 종료할 수 있도록 알립니다.
"""

from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket


class SingleInstanceManager(QObject):
    """QLocalServer/QLocalSocket 기반 단일 인스턴스 관리자."""

    DEFAULT_APP_ID = "EduFlow_SchoolWorkstation_SingleInstance"
    SHOW_COMMAND = b"SHOW"

    show_window_requested = pyqtSignal()

    def __init__(
        self,
        app_id: str = DEFAULT_APP_ID,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.app_id = app_id
        self._server: Optional[QLocalServer] = None

    def is_already_running(self) -> bool:
        """이미 실행 중인 인스턴스에 연결을 시도하고, 연결 성공 시 'SHOW' 명령을 전송합니다."""
        socket = QLocalSocket(self)
        socket.connectToServer(self.app_id)
        if socket.waitForConnected(500):
            socket.write(self.SHOW_COMMAND)
            socket.flush()
            socket.waitForBytesWritten(500)
            socket.disconnectFromServer()
            if socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
                socket.waitForDisconnected(200)
            socket.close()
            return True

        socket.abort()
        socket.close()
        return False

    def start_server(self) -> bool:
        """기존 잔류 소켓/파이프를 정리한 뒤 단일 인스턴스 로컬 서버를 시작합니다."""
        if self._server is not None:
            return True

        # 이전 비정상 종료로 남아있을 수 있는 소켓/파이프 정리
        QLocalServer.removeServer(self.app_id)

        server = QLocalServer(self)
        server.newConnection.connect(self._on_new_connection)
        if not server.listen(self.app_id):
            print(f"[SingleInstance] 서버 시작 실패 ({self.app_id}): {server.errorString()}")
            server.close()
            server.deleteLater()
            return False

        self._server = server
        return True

    def _on_new_connection(self) -> None:
        """새 클라이언트 연결을 수락하고 수신 데이터를 처리합니다."""
        if self._server is None:
            return

        while self._server.hasPendingConnections():
            client_socket = self._server.nextPendingConnection()
            if client_socket is None:
                continue

            client_socket.disconnected.connect(client_socket.deleteLater)
            client_socket.readyRead.connect(
                lambda sock=client_socket: self._read_client_message(sock)
            )
            if client_socket.bytesAvailable() > 0:
                self._read_client_message(client_socket)

    def _read_client_message(self, client_socket: QLocalSocket) -> None:
        """클라이언트가 보낸 명령을 읽어 창 복원 시그널을 발생시킵니다."""
        data = bytes(client_socket.readAll())
        if self.SHOW_COMMAND in data:
            self.show_window_requested.emit()
            client_socket.disconnectFromServer()

    def close(self) -> None:
        """로컬 서버를 종료하고 소켓/파이프 리소스를 정리합니다."""
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None
            QLocalServer.removeServer(self.app_id)
