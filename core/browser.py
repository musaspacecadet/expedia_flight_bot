from typing import Dict, List, Optional, Any
import logging
from .node import Tab
from .control import CDPController, DisplayManager, DisplayConfig
from .launcher import Launcher
from cdp import target, browser as cdp_browser

class Browser(CDPController):
    """
    A high-level browser controller that manages DOMControllers as tabs.
    Extends CDPController to handle low-level CDP communication.
    """
    
    def __init__(self, websocket_url: str):
        super().__init__(websocket_url)
        self.logger = logging.getLogger("Browser")
        self.tabs: Dict[str, Tab] = {}
        self.active_tab_id: Optional[str] = None

    def start(self) -> None:
        """Start the browser and initialize display"""
        self.connect()
        self.setup_target_discovery()
        
        # Get existing tabs
        existing_targets = self.get_targets()
        for target_info in existing_targets:
            if target_info.type_ == "page":
                self._create_tab(target_info.target_id)

    def stop(self) -> None:
        """Stop the browser and cleanup"""
        for controller in self.tabs.values():
            controller.disconnect()
        self.tabs.clear()
        self.disconnect()

    def _create_tab(self, target_id: str) -> Tab:
        """Create a new DOM controller for a tab"""
        ws_url = self.get_websocket_debugger_url(target_id)
        controller = Tab(ws_url, target_id)
        controller.connect()
        self.tabs[target_id] = controller
        
        # Make this the active tab if we don't have one
        if not self.active_tab_id:
            self.active_tab_id = target_id
            
        return controller

    def new_tab(self, url: str = "about:blank") -> Tab:
        """Create a new tab and return its controller"""
        target_id = self.execute_command(target.create_target(url=url))
        controller = self._create_tab(target_id)
        return controller

    def close_tab(self, target_id: str) -> None:
        """Close a specific tab"""
        if target_id in self.tabs:
            self.tabs[target_id].disconnect()
            self.execute_command(target.close_target(target_id=target_id))
            del self.tabs[target_id]
            
            # If we closed the active tab, activate another one if available
            if self.active_tab_id == target_id:
                self.active_tab_id = next(iter(self.tabs)) if self.tabs else None

    def switch_to(self, target_id: str) -> Tab:
        """Switch to a tab and return its controller"""
        if target_id not in self.tabs:
            raise ValueError(f"Tab {target_id} not found")
        
        self.active_tab_id = target_id
        self.execute_command(target.activate_target(target_id=target_id))
        return self.tabs[target_id]

    @property
    def active_tab(self) -> Optional[Tab]:
        """Get the currently active tab's controller"""
        return self.tabs.get(self.active_tab_id) if self.active_tab_id else None

    def handle_target_created(self, event: target.TargetCreated) -> None:
        """Handle new target creation"""
        if event.target_info.type_ == "page":
            self._create_tab(event.target_info.target_id)

    def handle_target_destroyed(self, event: target.TargetDestroyed) -> None:
        """Handle target destruction"""
        target_id = event.target_id
        if target_id in self.tabs:
            self.tabs[target_id].disconnect()
            del self.tabs[target_id]

    def __enter__(self) -> 'Browser':
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()