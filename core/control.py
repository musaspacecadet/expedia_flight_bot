# red.py
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import threading
import time
from urllib.parse import urlparse
import websocket
from typing import Dict, Optional, Any, TypeVar, Type, Callable, List
from dataclasses import dataclass
from pyvirtualdisplay import Display
from cdp.util import _event_parsers, parse_json_event
import cdp
import requests

T = TypeVar('T')

@dataclass
class DisplayConfig:
    """Configuration for virtual display"""
    width: int = 1920
    height: int = 1080
    depth: int = 24
    visible: bool = False
    backend: str = None

class DisplayManager:
    """Manages virtual display creation and lifecycle"""
    
    def __init__(self, config: Optional[DisplayConfig] = None):
        self.config = config or DisplayConfig()
        self._display: Optional[Display] = None
        self.logger = logging.getLogger("DisplayManager")

    def start(self) -> bool:
        try:
            self._display = Display(
                visible=self.config.visible,
                size=(self.config.width, self.config.height),
                color_depth=self.config.depth,
                backend=self.config.backend
            )
            self._display.start()
            self.logger.info(f"Started virtual display: {self.config.width}x{self.config.height}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to start display: {e}")
            return False

    def stop(self) -> None:
        if self._display:
            try:
                self._display.stop()
                self.logger.info("Stopped virtual display")
            except Exception as e:
                self.logger.error(f"Error stopping display: {e}")
            finally:
                self._display = None

    def __enter__(self) -> 'DisplayManager':
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

class CDPConnectionError(Exception):
    """Custom exception for CDP connection issues"""
    pass

class Session:
    """Represents a CDP session with a target"""
    
    def __init__(self, session_id: str, target_id: str):
        self.session_id = session_id
        self.target_id = target_id
        self.active = True

    def deactivate(self):
        self.active = False

class CDPController:
    """
    Controls Chrome DevTools Protocol communication.
    Handles command execution with session support and event callbacks.
    """

    def __init__(self, websocket_url: str, target_id: Optional[str] = None, connection_timeout: float = 10.0, **kwargs):
        self.ws_url = websocket_url
        self.connection_timeout = connection_timeout
        self.logger = logging.getLogger("CDPController")
        self._command_id = 0
        self._websocket: Optional[websocket.WebSocket] = None
        self._command_events: Dict[int, threading.Event] = {}
        self._command_results: Dict[int, Any] = {}
        self._lock = threading.Lock()
        self._sessions: Dict[str, Session] = {}
        self._current_session: Optional[Session] = None
        self._event_listeners: Dict[str, List[Callable[[Any], None]]] = {}
        self._running = False
        self._connection_event = threading.Event()
        self.target_id = target_id  # Store the targetId
        self.target_controllers: Dict[str, 'CDPController'] = {} # target_id to CDPController



    def connect(self) -> None:
        """Connect to Chrome DevTools WebSocket with timeout and retry logic"""
        start_time = time.time()
        while time.time() - start_time < self.connection_timeout:
            try:
                self._websocket = websocket.create_connection(
                    self.ws_url,
                    timeout=5
                )
                self._websocket.settimeout(1)
                self._running = True
                self._connection_event.set()
                
                # Start message receiver thread
                self._receiver_thread = threading.Thread(
                    target=self._receive_messages,
                    name="cdp_receiver",
                    daemon=True
                )
                self._receiver_thread.start()
                
                self.logger.info(f"Connected to Chrome DevTools at {self.ws_url}")
                return
            except Exception as e:
                self.logger.warning(f"Connection attempt failed: {e}")
                time.sleep(1)
        
        raise CDPConnectionError(f"Failed to connect after {self.connection_timeout} seconds")

    def disconnect(self) -> None:
        """Disconnect from Chrome DevTools gracefully"""
        self._running = False
        self._connection_event.clear()
        
        # Close all active sessions
        for session in self._sessions.values():
            session.deactivate()

        # Disconnect all target controllers
        for target_controller in self.target_controllers.values():
            target_controller.disconnect()
        self.target_controllers.clear()
        
        if self._websocket:
            try:
                self._websocket.close()
            except Exception as e:
                self.logger.warning(f"Error closing websocket: {e}")
            finally:
                self._websocket = None
            
        if self._receiver_thread:
            self._receiver_thread.join(timeout=2)
            
        # Clean up any pending commands
        with self._lock:
            for event in self._command_events.values():
                event.set()
            self._command_events.clear()
            self._command_results.clear()
            
        self.logger.info("Disconnected from Chrome DevTools")

    def _receive_messages(self) -> None:
        """Background thread to receive command responses and events with improved error handling"""
        while self._running and self._websocket:
            try:
                message = self._websocket.recv()
                if not message:
                    continue
                    
                try:
                    data = json.loads(message)
                except json.JSONDecodeError as e:
                    self.logger.error(f"Invalid JSON received: {e}")
                    continue
                
                if 'id' in data:
                    # Handle command responses
                    cmd_id = data['id']
                    with self._lock:
                        if cmd_id in self._command_events:
                            self._command_results[cmd_id] = data
                            self._command_events[cmd_id].set()
                else:
                    # Handle event messages
                    self._handle_event(data)
                    
            except websocket.WebSocketTimeoutException:
                continue
            except websocket.WebSocketConnectionClosedException:
                if self._running:
                    self.logger.error("WebSocket connection closed unexpectedly")
                    self._handle_connection_lost()
                break
            except Exception as e:
                if self._running:
                    self.logger.error(f"Error in message receiver: {e}")
                break

    def _handle_connection_lost(self):
        """Handle unexpected connection loss"""
        self._connection_event.clear()
        # Notify pending commands
        with self._lock:
            for event in self._command_events.values():
                event.set()

    def _handle_event(self, data: dict) -> None:
        """Internal method to handle CDP events and dispatch to listeners with error handling"""
        method = data.get("method")
        params = data.get("params")
        if not method:
            return
            
        listeners = self._event_listeners.get(method, [])
        for callback in listeners:
            try:
                event = parse_json_event(data)
                callback(event)
            except Exception as e:
                self.logger.error(f"Error in event callback for {method}: {e}", exc_info=True)

    def get_event_name(self, event_cls) -> str:
        """
        Converts an event class type to its registered event name.
        
        Args:
            event_cls: The event class type
            
        Returns:
            str: The CDP event name
            
        Raises:
            ValueError: If the event class is not registered
        """
        for method, cls in _event_parsers.items():
            if cls == event_cls:
                return method
        raise ValueError(f"Event class {event_cls.__name__} is not registered")

    def add_event_listener(self, event_type: type, callback: Callable[[Any], None]) -> None:
        """
        Register a callback for a specific event with validation.
        
        Args:
            event_type: The event class type
            callback: Function to call when the event is received
        """
        try:
            event_name = self.get_event_name(event_type)
        except ValueError as e:
            self.logger.error(f"Failed to add event listener: {e}")
            return
            
        with self._lock:
            if event_name not in self._event_listeners:
                self._event_listeners[event_name] = []
            if callback not in self._event_listeners[event_name]:
                self._event_listeners[event_name].append(callback)
                self.logger.info(f"Added event listener for {event_name}")
            else:
                self.logger.warning(f"Callback already registered for {event_name}")

    def remove_event_listener(self, event_type: type, callback: Callable[[Any], None]) -> None:
        """
        Remove a previously registered event callback with validation.
        
        Args:
            event_type: The event class type
            callback: The callback function to remove
        """
        try:
            event_name = self.get_event_name(event_type)
        except ValueError as e:
            self.logger.error(f"Failed to remove event listener: {e}")
            return
            
        with self._lock:
            if event_name in self._event_listeners:
                try:
                    self._event_listeners[event_name].remove(callback)
                    self.logger.info(f"Removed event listener for {event_name}")
                except ValueError:
                    self.logger.warning(f"Callback not found for event {event_name}")

    def attach_to_target(self, target_id: str, timeout: float = 5.0) -> str:
        """
        Attach to a target and create a new session with timeout.

        Args:
            target_id: Target ID to attach to
            timeout: Maximum time to wait for attachment

        Returns:
            Session ID

        Raises:
            TimeoutError: If attachment takes too long
            RuntimeError: If attachment fails
        """
        try:
            session_id = self.execute_command(
                cdp.target.attach_to_target(target_id=target_id, flatten=True),
                timeout=timeout,
                session=False
            ).session_id # type: ignore
        except Exception as e:
            raise RuntimeError(f"Failed to attach to target: {e}")

        session = Session(session_id, target_id)
        with self._lock:
            self._sessions[session_id] = session
            self._current_session = session
        return session_id

    def detach_from_target(self, session_id: str) -> None:
        """
        Detach from a target session and clean up.
        
        Args:
            session_id: The session ID to detach from
        """
        try:
            # Execute the detach command
            self.execute_command(
                cdp.target.detach_from_target(session_id=session_id),
                timeout=5.0,
                session=False
            )
            
            # Clean up session
            with self._lock:
                if session_id in self._sessions:
                    self._sessions[session_id].deactivate()
                    del self._sessions[session_id]
                    
                # Reset current session if it was the detached one
                if self._current_session and self._current_session.session_id == session_id:
                    self._current_session = None
                    
        except Exception as e:
            self.logger.warning(f"Error detaching from session {session_id}: {e}")

    def set_current_session(self, session_id: str) -> None:
        """
        Set the current session for command execution with validation.

        Args:
            session_id: Session ID to set as current

        Raises:
            ValueError: If session_id is invalid or session is inactive
        """
        with self._lock:
            if session_id not in self._sessions:
                raise ValueError(f"No session found with ID: {session_id}")
            session = self._sessions[session_id]
            if not session.active:
                raise ValueError(f"Session {session_id} is no longer active")
            self._current_session = session

    def execute_command(self, cmd_generator: Any, timeout: float = 30.0, session=True) -> Any:
        """
        Execute a CDP command using the generator pattern synchronously with timeout.

        Args:
            cmd_generator: Generator from a CDP command function
            timeout: Maximum time to wait for command completion

        Returns:
            Command result

        Raises:
            RuntimeError: If command execution fails
            TimeoutError: If command execution takes too long
        """
        if not self._websocket:
            raise RuntimeError("Not connected to Chrome DevTools")

        try:
            request = next(cmd_generator)
        except StopIteration:
            raise RuntimeError("Command generator did not yield a request")

        with self._lock:
            self._command_id += 1
            current_id = self._command_id
            request['id'] = current_id

            # Add session ID if we have a current session
            if session:
                if self._current_session and self._current_session.active:
                    request['sessionId'] = self._current_session.session_id

            event = threading.Event()
            self._command_events[current_id] = event

        try:
            payload = json.dumps(request)
            self._websocket.send(payload)

            if not event.wait(timeout):
                raise TimeoutError(f"Command timed out after {timeout} seconds")

            with self._lock:
                response = self._command_results.pop(current_id, None)
                self._command_events.pop(current_id, None)

            if response is None:
                raise RuntimeError("No response received for command")
            if 'error' in response:
                raise Exception(response['error'])

            try:
                result = cmd_generator.send(response["result"])
                raise RuntimeError("Command generator did not stop as expected")
            except StopIteration as si:
                return si.value

        except Exception as e:
            self.logger.error(f"Command execution failed: {e}")
            raise
    
    def get_websocket_debugger_url(self, target_id: str) -> str:
        """Gets the WebSocket debugger URL for a specific target ID.""" 
        parsed_url = urlparse(self.ws_url)
        base_url = f"http://{parsed_url.hostname}:{parsed_url.port}/json/list"

        response = requests.get(url=base_url)
        targets = response.json()
        for target_info in targets:
            if target_info['id'] == target_id:
                return target_info['webSocketDebuggerUrl']
        raise ValueError(f"No WebSocket debugger URL found for target ID: {target_id}")

    def get_targets(self) -> List[cdp.target.TargetInfo]:
        """Return a list of the current targets."""
        return self.execute_command(cdp.target.get_targets())
    

    def handle_target_created(self, event: cdp.target.TargetCreated) -> None:
        """
        Handles the targetCreated event.  Creates a new CDPController for the
        new target if it's a "page".
        """
        if event.target_info.type_ == "page":
            self.logger.info(f"New page target created: {event.target_info.target_id}")
            self.logger.info(f"Target Info: {event.target_info}")
            try:
                ws_url = self.get_websocket_debugger_url(event.target_info.target_id)
                new_controller = CDPController(ws_url, target_id=event.target_info.target_id)
                new_controller.connect()
                self.target_controllers[event.target_info.target_id] = new_controller
                self.logger.info(f"CDPController created and connected for target: {event.target_info.target_id}")

                #  Optionally, you could immediately attach to the target here:
                # new_controller.attach_to_target(event.target_info.target_id)

            except Exception as e:
                self.logger.error(f"Failed to create CDPController for target {event.target_info.target_id}: {e}")

    def handle_target_destroyed(self, event: cdp.target.TargetDestroyed) -> None:
        """Handles the targetDestroyed event.  Cleans up the CDPController."""
        target_id = event.target_id
        if target_id in self.target_controllers:
            self.logger.info(f"Target destroyed: {target_id}")
            controller = self.target_controllers.pop(target_id)
            controller.disconnect()
        else:
            self.logger.warning(f"Received targetDestroyed for unknown target: {target_id}")

    def setup_target_discovery(self) -> None:
        """Sets up target discovery and event listeners."""
        self.execute_command(cdp.target.set_discover_targets(discover=True))
        self.add_event_listener(cdp.target.TargetCreated, self.handle_target_created)
        self.add_event_listener(cdp.target.TargetDestroyed, self.handle_target_destroyed)
        self.logger.info("Target discovery enabled.")

    def __enter__(self) -> 'CDPController':
        self.connect()
        if not self.target_id:  # Only the initial controller sets up discovery
            self.setup_target_discovery()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.disconnect()