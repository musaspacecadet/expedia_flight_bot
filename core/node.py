# tab.py
import base64
import logging
import threading
import time
from typing import Dict, Optional, Any, Callable, List
from urllib.parse import urlparse

import cdp
from cdp.util import parse_json_event
from .control import CDPController, CDPConnectionError  # Import from the correct location


class Position(cdp.dom.Quad):
    """helper class for element positioning"""

    def __init__(self, points):
        super().__init__(points)
        (
            self.left,
            self.top,
            self.right,
            self.top,
            self.right,
            self.bottom,
            self.left,
            self.bottom,
        ) = points
        self.abs_x: float = 0
        self.abs_y: float = 0
        self.x = self.left
        self.y = self.top
        self.height, self.width = (self.bottom - self.top, self.right - self.left)
        self.center = (
            self.left + (self.width / 2),
            self.top + (self.height / 2),
        )

    def to_viewport(self, scale=1):
        return cdp.page.Viewport(
            x=self.x, y=self.y, width=self.width, height=self.height, scale=scale
        )

    def __repr__(self):
        return f"<Position(x={self.left}, y={self.top}, width={self.width}, height={self.height})>"


class Element:
    """
    Represents a DOM element and provides methods for interacting with it.
    """

    def __init__(self, node_id: cdp.dom.NodeId, backend_node_id: cdp.dom.BackendNodeId, object_id: str, tab: 'Tab'):
        self.node_id = node_id
        self.backend_node_id = backend_node_id
        self.object_id = object_id
        self._tab = tab
        self.logger = logging.getLogger("Element")

    def _resolve_object(self) -> cdp.runtime.RemoteObject:
        """Resolves the remote object for this element."""
        if not self.object_id:
            raise ValueError("Element does not have a valid object_id")
        return self._tab.execute_command(cdp.dom.resolve_node(backend_node_id=self.backend_node_id))

    def query_selector(self, selector: str) -> Optional['Element']:
        """
        Finds the first child element matching the given CSS selector.

        Args:
            selector: The CSS selector.

        Returns:
            The Element, or None if not found.
        """
        try:
            node_id = self._tab.execute_command(cdp.dom.query_selector(node_id=self.node_id, selector=selector))
            if node_id == 0:  # NodeId 0 indicates not found
                return None
            
            resolved_node = self._tab.resolve_node(node_id)
            
            if not resolved_node:
                return None
            node = self._tab.execute_command(cdp.dom.describe_node(node_id=node_id))
            if node is None:
                return None
            return Element(node_id, node.backend_node_id, resolved_node.object_id, self._tab)
        except Exception as e:
            self.logger.error(f"Error in query_selector: {e}")
            return None
    
    def query_selector_all(self, selector: str) -> List['Element']:
        """
        Finds all child elements matching the given CSS selector.

        Args:
            selector: The CSS selector.

        Returns:
            A list of Elements.
        """
        try:
            node_ids = self._tab.execute_command(cdp.dom.query_selector_all(node_id=self.node_id, selector=selector))
            elements = []
            for node_id in node_ids:
                resolved_node = self._tab.resolve_node(node_id)
                node = self._tab.execute_command(cdp.dom.describe_node(node_id=node_id))
                if resolved_node:
                    elements.append(Element(node_id, node.backend_node_id, resolved_node.object_id, self._tab))
            return elements
        except Exception as e:
            self.logger.error(f"Error in query_selector_all: {e}")
            return []
    
    def xpath(self, xpath_expression: str) -> Optional['Element']:
        """
        Finds the first child element matching the given XPath expression.

        Args:
            xpath_expression: The XPath expression.

        Returns:
            The Element, or None if not found.
        """
        try:
            obj = self._resolve_object()
            result, exception_logs = self._tab.execute_command(cdp.runtime.call_function_on(
                function_declaration=f"""
                function() {{
                    const result = document.evaluate(
                        '{xpath_expression}',
                        this,
                        null,
                        XPathResult.FIRST_ORDERED_NODE_TYPE,
                        null
                    );
                    return result.singleNodeValue;
                }}
                """,
                object_id=obj.object_id,
                return_by_value=False
            ))
            
            if exception_logs or not result or not result.object_id:
                if exception_logs:
                    self.logger.error(f"XPath error: {exception_logs}")
                return None
            
            # Now we need to get the node ID from the result object
            remote_obj_id = result.object_id
            node = self._tab.execute_command(cdp.dom.request_node(object_id=remote_obj_id))
            
            if not node:
                return None
                
            resolved_node = self._tab.resolve_node(node)
            if not resolved_node:
                return None
                
            node_info = self._tab.execute_command(cdp.dom.describe_node(node_id=node))
            
            return Element(node, node_info.backend_node_id, resolved_node.object_id, self._tab)
        except Exception as e:
            self.logger.error(f"Error in xpath: {e}")
            return None
    
    def xpath_all(self, xpath_expression: str) -> List['Element']:
        """
        Finds all child elements matching the given XPath expression.

        Args:
            xpath_expression: The XPath expression.

        Returns:
            A list of Elements.
        """
        try:
            obj = self._resolve_object()
            result, exception_logs = self._tab.execute_command(cdp.runtime.call_function_on(
                function_declaration=f"""
                function() {{
                    const result = document.evaluate(
                        '{xpath_expression}',
                        this,
                        null,
                        XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
                        null
                    );
                    let nodes = [];
                    for (let i = 0; i < result.snapshotLength; i++) {{
                        nodes.push(result.snapshotItem(i));
                    }}
                    return nodes;
                }}
                """,
                object_id=obj.object_id,
                return_by_value=False
            ))
            
            if exception_logs or not result or not result.object_id:
                if exception_logs:
                    self.logger.error(f"XPath error: {exception_logs}")
                return []
            
            # Now we need to get the node IDs from the array
            elements = []
            
            # Get the length of the array
            length_result, length_exception = self._tab.execute_command(cdp.runtime.call_function_on(
                function_declaration="function() { return this.length; }",
                object_id=result.object_id,
                return_by_value=True
            ))
            
            if length_exception or not length_result:
                return []
                
            length = int(length_result.value)
            
            # Process each element in the array
            for i in range(length):
                item_result, item_exception = self._tab.execute_command(cdp.runtime.call_function_on(
                    function_declaration=f"function() {{ return this[{i}]; }}",
                    object_id=result.object_id,
                    return_by_value=False
                ))
                
                if item_exception or not item_result or not item_result.object_id:
                    continue
                    
                # Convert DOM node to node ID
                node = self._tab.execute_command(cdp.dom.request_node(object_id=item_result.object_id))
                
                if not node:
                    continue
                    
                resolved_node = self._tab.resolve_node(node)
                if not resolved_node:
                    continue
                    
                node_info = self._tab.execute_command(cdp.dom.describe_node(node_id=node))
                
                elements.append(Element(node, node_info.backend_node_id, resolved_node.object_id, self._tab))
            
            return elements
        except Exception as e:
            self.logger.error(f"Error in xpath_all: {e}")
            return []
    
    def find_child_with_text(self, text: str) -> Optional['Element']:
        """
        Finds a child element containing the specified text.

        Args:
            text: The text to search for.

        Returns:
            The Element, or None if not found.
        """
        try:
            # Using XPath to find elements containing text is more efficient
            return self.xpath(f".//*/text()[contains(., '{text}')]/parent::*")
        except Exception as e:
            self.logger.error(f"Error in find_child_with_text: {e}")
            return None
    
    def find_child_with_exact_text(self, text: str) -> Optional['Element']:
        """
        Finds a child element with the exact specified text.

        Args:
            text: The exact text to search for.

        Returns:
            The Element, or None if not found.
        """
        try:
            # Using XPath to find elements with exact text
            return self.xpath(f".//*/text()[. = '{text}']/parent::*")
        except Exception as e:
            self.logger.error(f"Error in find_child_with_exact_text: {e}")
            return None
    
    def get_children(self) -> List['Element']:
        """
        Gets all direct child elements of this element.

        Returns:
            A list of child Elements.
        """
        try:
            # Using XPath to get direct children is more reliable across browsers
            return self.xpath_all("./*")
        except Exception as e:
            self.logger.error(f"Error in get_children: {e}")
            return []
    
    def click(self, delay_after_click: float = 0.0, scroll_to_view: bool = True) -> None:
        """
        Clicks the element.

        Args:
            delay_after_click: Time to wait after the click (in seconds).
            scroll_to_view: Whether to scroll the element into view before clicking.
        """
        obj = self._resolve_object()
        if scroll_to_view:
            self.scroll_into_view()
        arguments = [cdp.runtime.CallArgument(object_id=obj.object_id)]
        
        self._tab.execute_command(
            cdp.runtime.call_function_on(
                "(el) => el.click()",
                object_id=obj.object_id,
                arguments=arguments,
                await_promise=True,
                user_gesture=True,
                return_by_value=True,
            )
        )
        if delay_after_click > 0:
            self._tab._wait(delay_after_click)  # Use _wait from Tab

    def type_text(self, text: str, delay_after_typing: float = 0.0) -> None:
        """
        Types text into the element (e.g., an input field).

        Args:
            text: The text to type.
            delay_after_typing: Time to wait after typing (in seconds).
        """
        obj = self._resolve_object()
        self._tab.execute_command(cdp.dom.focus(object_id=obj.object_id))
        for char in text:
            self._tab.execute_command(cdp.input_.dispatch_key_event(
                type_="char",
                text=char
            ))
        if delay_after_typing > 0:
            self._tab._wait(delay_after_typing)
    
    def clear_text(self) -> None:
        """
        Clears the text content of an input element 
        
        Returns:
            None
        """
        self.execute_js("this.value = '';")
        

    def get_attribute_value(self, name: str) -> Optional[str]:
        """
        Gets the value of an attribute of the element.

        Args:
            name: The name of the attribute.

        Returns:
            The attribute value, or None if not found.
        """
        obj = self._resolve_object()
        result , exception_logs = self._tab.execute_command(cdp.runtime.call_function_on(
            function_declaration=f"function() {{ return this.getAttribute('{name}'); }}",
            object_id=obj.object_id,
            return_by_value=True
        ))
        return result.value  # type: ignore

    def get_property_value(self, name: str) -> Any:
        """
        Gets the value of a JavaScript property of the element.

        Args:
            name: The name of the property.

        Returns:
            The property value.
        """
        obj = self._resolve_object()
        result, exception_logs = self._tab.execute_command(cdp.runtime.call_function_on(
            function_declaration=f"function() {{ return this.{name}; }}",
            object_id=obj.object_id,
            return_by_value=True
        ))
        return result.value  # type: ignore

    def get_content(self) -> str:
        """
        Gets the inner text content of the element.

        Returns:
            The inner text content.
        """
        obj = self._resolve_object()
        result, exception_logs = self._tab.execute_command(cdp.runtime.call_function_on(
            function_declaration="function() { return this.textContent; }",
            object_id=obj.object_id,
            return_by_value=True
        ))
        return result.value  # type: ignore

    def get_inner_html(self) -> str:
        """
        Gets the innerHTML of the element.

        Returns:
            The inner HTML content.
        """
        obj = self._resolve_object()
        result, exception_logs = self._tab.execute_command(cdp.runtime.call_function_on(
            function_declaration="function() { return this.innerHTML; }",
            object_id=obj.object_id,
            return_by_value=True
        ))
        return result.value  # type: ignore

    def get_outer_html(self) -> str:
        """
        Gets the outerHTML of the element.

        Returns:
            The outer HTML content.
        """
        obj = self._resolve_object()
        result, exception_logs = self._tab.execute_command(cdp.runtime.call_function_on(
            function_declaration="function() { return this.outerHTML; }",
            object_id=obj.object_id,
            return_by_value=True
        ))
        return result.value  # type: ignore

    def scroll_into_view(self) -> None:
        """Scrolls the element into view."""
        obj = self._resolve_object()
        self._tab.execute_command(cdp.dom.scroll_into_view_if_needed(object_id=obj.object_id))
    

    def screenshot2(self, 
                    path: str, 
                    format: str = "png",
                    quality: Optional[int] = None,
                    from_surface: bool = True,
                    capture_beyond_viewport: bool = True,
                    optimize_for_speed: bool = True
                ) -> bytes:
      
        obj = self._resolve_object()
        # Get element's position and dimensions using the injected script
        result, exception_logs = self._tab.execute_command(cdp.runtime.call_function_on(
            function_declaration="""
                function() {
                    const rect = this.getBoundingClientRect();
                    return {
                        x: rect.left + window.scrollX,
                        y: rect.top + window.scrollY,
                        width: rect.width,
                        height: rect.height
                    };
                }
            """,
            object_id=obj.object_id,
            return_by_value=True
        ))
        
        if exception_logs or not result:
            return None, "Failed to get element dimensions"
        
        # Capture screenshot of the specific area
        dimensions = result.value
        viewport = cdp.page.Viewport.from_json({
                'x': dimensions['x'],
                'y': dimensions['y'],
                'width': dimensions['width'],
                'height': dimensions['height'],
                'scale': 1
            })

        data = self._tab.execute_command(
            cdp.page.capture_screenshot(
                format_=format,
                quality=quality,
                clip=viewport,
                from_surface=from_surface,
                capture_beyond_viewport=capture_beyond_viewport,
                optimize_for_speed=optimize_for_speed
            )
        )

        data_bytes = base64.b64decode(data)
        with open(path, "wb") as file:
            file.write(data_bytes)
        
        return data_bytes
        
    
    def screenshot(self, 
                    path: str, 
                    format: str = "png",
                    quality: Optional[int] = None,
                    from_surface: bool = True,
                    capture_beyond_viewport: bool = True,
                    optimize_for_speed: bool = True
                ) -> bytes:
        position = self.get_position()
        if not position:    
            raise ValueError("Could not get position of element.")
        viewport = position.to_viewport()
        self.scroll_into_view()
        data = self._tab.execute_command(
            cdp.page.capture_screenshot(
                format_=format,
                quality=quality,
                clip=viewport,
                from_surface=from_surface,
                capture_beyond_viewport=capture_beyond_viewport,
                optimize_for_speed=optimize_for_speed
            )
        )
        if not data:
            raise ValueError("Could not capture screenshot of element.")

        data_bytes = base64.b64decode(data)
        with open(path, "wb") as file:
            file.write(data_bytes)
        
        return data_bytes

    def get_position(self, absolute=False):
        obj = self._resolve_object()

        try:
            quads = self._tab.execute_command(
                cdp.dom.get_content_quads(object_id=obj.object_id)
            )
            if not quads:
                raise ValueError("Could not get content quads for element.")
            pos = Position(quads[0])

            if absolute:
                # Get scroll position (scrollX and scrollY) in one JavaScript function call
                result, exception_logs = self._tab.execute_command(cdp.runtime.call_function_on(
                    function_declaration="""
                        function() {
                            return {
                                scrollX: window.scrollX,
                                scrollY: window.scrollY
                            };
                        }
                    """,
                    object_id=obj.object_id,
                    return_by_value=True
                ))

                # Retrieve scrollX and scrollY from the result
                if result:
                    scroll_x = result.value['scrollX']
                    scroll_y = result.value['scrollY']

                    # Use the scroll values for further calculations in Python
                    pos.abs_x = pos.left + scroll_x + (pos.width / 2)
                    pos.abs_y = pos.top + scroll_y + (pos.height / 2)

            return pos
        except IndexError:
            pass

    @property
    def bounding_box(self) -> cdp.dom.Rect:
        """Gets the bounding box of the element."""
        return self._tab.get_bounding_box(self.backend_node_id)

    def execute_js(self, script: str, *args, return_by_value: bool = True) -> Any:
        """
        Executes JavaScript in the context of this element.
        
        Args:
            script: JavaScript function body as string. Will be wrapped in function.
            *args: Arguments to pass to the function.
            return_by_value: Whether to return the result by value.
            
        Returns:
            The result of the JavaScript execution.
        """
        obj = self._resolve_object()
        
        # Convert Python args to CDP CallArguments
        call_args = []
        for arg in args:
            if isinstance(arg, Element):
                # If arg is another Element, pass its object_id
                arg_obj = arg._resolve_object()
                call_args.append(cdp.runtime.CallArgument(object_id=arg_obj.object_id))
            else:
                # Otherwise pass by value
                call_args.append(cdp.runtime.CallArgument(value=arg))
        
        # Create a function that takes 'this' as the first parameter
        function_declaration = f"function() {{ {script} }}"
        
        result, exception_logs = self._tab.execute_command(cdp.runtime.call_function_on(
            function_declaration=function_declaration,
            object_id=obj.object_id,
            arguments=call_args,
            return_by_value=return_by_value
        ))
        
        if exception_logs:
            self.logger.error(f"Error executing JS: {exception_logs}")
        
        return result.value if return_by_value else result
    
    def is_visible(self) -> bool:
        """
        Checks if the element is visible in the DOM.
        
        Returns:
            True if the element is visible, False otherwise.
        """
        obj = self._resolve_object()
        result, exception_logs = self._tab.execute_command(cdp.runtime.call_function_on(
            function_declaration="""
                function() {
                    const style = window.getComputedStyle(this);
                    return !(
                        style.display === 'none' || 
                        style.visibility === 'hidden' || 
                        style.opacity === '0' ||
                        this.offsetWidth === 0 ||
                        this.offsetHeight === 0
                    );
                }
            """,
            object_id=obj.object_id,
            return_by_value=True
        ))
        
        return bool(result.value) if result else False
    
    def wait_for_visibility(self, timeout: float = 10.0, check_interval: float = 0.1) -> bool:
        """
        Waits for the element to become visible.
        
        Args:
            timeout: Maximum time to wait in seconds.
            check_interval: Time between visibility checks in seconds.
            
        Returns:
            True if element became visible within timeout, False otherwise.
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.is_visible():
                return True
            time.sleep(check_interval)
        return False

    def __repr__(self) -> str:
        return f"<Element node_id={self.node_id}, backend_node_id={self.backend_node_id}, object_id={self.object_id}>"
    

class Tab(CDPController):
    """
    Represents a single browser tab and provides methods for interacting with it.
    Extends CDPController to handle the WebSocket connection for the tab.
    """

    def __init__(self, websocket_url: str, target_id: str, connection_timeout: float = 10.0):
        super().__init__(websocket_url, target_id, connection_timeout)
        self.logger = logging.getLogger("Tab")
        self._dom_enable_count = 0  # Track DOM.enable calls
        self._page_enable_count = 0 # Track Page.enable calls
        self._runtime_enable_count = 0 # Track Runtime.enable calls

    def _wait(self, seconds: float) -> None:
        """Waits for the specified number of seconds."""
        if seconds > 0:
            self.logger.debug(f"Waiting for {seconds} seconds...")
            time.sleep(seconds)

    def navigate(self, url: str, wait_for_load: bool = True, timeout: float = 30.0) -> None:
        """
        Navigates the tab to the specified URL.

        Args:
            url: The URL to navigate to.
            wait_for_load: Whether to wait for the page to be interactive and fully loaded.
            timeout: Maximum time to wait for navigation.
        """
        self.logger.info(f"Navigating to: {url}")
        
        # Start navigating
        self.execute_command(cdp.page.navigate(url=url))

        if wait_for_load:

            # Wait for page to be interactive using a while loop
            start_time = time.time()
            while True:
                # Check the document.readyState in JS
                result, exception_logs = self.execute_command(cdp.runtime.evaluate(expression="document.readyState === 'complete'"))
                ready_state = result

                # If the page is interactive or complete, stop waiting
                
                if ready_state.value == True:
                    self.logger.info(f"Page is {ready_state}, interactive and ready.")
                    break

                # Check timeout
                elapsed_time = time.time() - start_time
                if elapsed_time > timeout:
                    self.logger.warning(f"Timeout reached while waiting for page to be interactive.")
                    break

                # Sleep for a short time to avoid excessive CPU usage
                time.sleep(0.1)


    def get_document_root(self) -> cdp.dom.Node:
        """Gets the root node of the DOM tree."""
        return self.execute_command(cdp.dom.get_document(depth=-1, pierce=True))

    def query_selector(self, selector: str) -> Optional[Element]:
        """
        Finds the first element matching the given CSS selector.

        Args:
            selector: The CSS selector.

        Returns:
            The Element, or None if not found.
        """
        root = self.get_document_root()
        try:
            node_id = self.execute_command(cdp.dom.query_selector(node_id=root.node_id, selector=selector))
            if node_id == 0:  # NodeId 0 indicates not found
                return None
            
            resolved_node = self.resolve_node(node_id)
            
            if not resolved_node:
                return None
            node : cdp.dom.Node = self.execute_command(cdp.dom.describe_node(node_id=node_id))
            if node is None:
                return None
            return Element(node_id, node.backend_node_id, resolved_node.object_id, self)  # type: ignore
        except Exception as e:
            self.logger.error(f"Error in query_selector: {e}")
            return None
    
    def find_element_with_text(self, text: str) -> Optional[Element]:
        search_id, nresult = self.execute_command(cdp.dom.perform_search(text, True))
        if nresult:
            node_ids = self.execute_command(cdp.dom.get_search_results(search_id, 0, nresult))
        else:
            node_ids = []
        self.execute_command(cdp.dom.discard_search_results(search_id))
        for node_id in node_ids:
            resolved_node = self.resolve_node(node_id)
            node : cdp.dom.Node = self.execute_command(cdp.dom.describe_node(node_id=node_id))
            if resolved_node:
                return Element(node_id, node.backend_node_id, resolved_node.object_id, self)
        return None

    def query_selector_all(self, selector: str) -> List[Element]:
        """
        Finds all elements matching the given CSS selector.

        Args:
            selector: The CSS selector.

        Returns:
            A list of Elements.
        """
        root = self.get_document_root()
        try:
            node_ids = self.execute_command(cdp.dom.query_selector_all(node_id=root.node_id, selector=selector))
            elements = []
            for node_id in node_ids:
                resolved_node = self.resolve_node(node_id)
                node : cdp.dom.Node = self.execute_command(cdp.dom.describe_node(node_id=node_id))
                if resolved_node:
                    elements.append(Element(node_id, node.backend_node_id, resolved_node.object_id, self))  # type: ignore
            return elements
        except Exception as e:
            self.logger.error(f"Error in query_selector_all: {e}")
            return []

    def resolve_node(self, node_id: cdp.dom.NodeId) -> Optional[cdp.runtime.RemoteObject]:
        """Resolves a node ID to a remote object."""
        try:
            remote_object = self.execute_command(cdp.dom.resolve_node(node_id=node_id))
            return remote_object  # type: ignore
        except Exception as e:
            self.logger.error(f"Error resolving node: {e}")
            return None
    
    def xpath(self, xpath_expression: str) -> Optional[Element]:
        """
        Finds the first element matching the given XPath expression.

        Args:
            xpath_expression: The XPath expression.

        Returns:
            The Element, or None if not found.
        """
        try:
            result, exception_logs = self.execute_command(cdp.runtime.evaluate(
                expression=f"""
                    (function() {{
                        const result = document.evaluate(
                            '{xpath_expression}',
                            document,
                            null,
                            XPathResult.FIRST_ORDERED_NODE_TYPE,
                            null
                        );
                        return result.singleNodeValue;
                    }})()
                """,
                return_by_value=False
            ))
            
            if exception_logs or not result or not result.object_id:
                if exception_logs:
                    self.logger.error(f"XPath error: {exception_logs}")
                return None
            
            # Now we need to get the node ID from the result object
            remote_obj_id = result.object_id
            node_id = self.execute_command(cdp.dom.request_node(object_id=remote_obj_id))
            
            if node_id == 0:
                return None
                
            resolved_node = self.resolve_node(node_id)
            if not resolved_node:
                return None
                
            node_info = self.execute_command(cdp.dom.describe_node(node_id=node_id))
            
            return Element(node_id, node_info.backend_node_id, resolved_node.object_id, self)
        except Exception as e:
            self.logger.error(f"Error in xpath: {e}")
            return None

    def xpath_all(self, xpath_expression: str) -> List[Element]:
        """
        Finds all elements matching the given XPath expression.

        Args:
            xpath_expression: The XPath expression.

        Returns:
            A list of Elements.
        """
        try:
            result, exception_logs = self.execute_command(cdp.runtime.evaluate(
                expression=f"""
                    (function() {{
                        const result = document.evaluate(
                            '{xpath_expression}',
                            document,
                            null,
                            XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
                            null
                        );
                        let nodes = [];
                        for (let i = 0; i < result.snapshotLength; i++) {{
                            nodes.push(result.snapshotItem(i));
                        }}
                        return nodes;
                    }})()
                """,
                return_by_value=False
            ))
            
            if exception_logs or not result or not result.object_id:
                if exception_logs:
                    self.logger.error(f"XPath error: {exception_logs}")
                return []
            
            # Now we need to get the node IDs from the array
            elements = []
            
            # Get the length of the array
            length_result, length_exception = self.execute_command(cdp.runtime.call_function_on(
                function_declaration="function() { return this.length; }",
                object_id=result.object_id,
                return_by_value=True
            ))
            
            if length_exception or not length_result:
                return []
                
            length = int(length_result.value)
            
            # Process each element in the array
            for i in range(length):
                item_result, item_exception = self.execute_command(cdp.runtime.call_function_on(
                    function_declaration=f"function() {{ return this[{i}]; }}",
                    object_id=result.object_id,
                    return_by_value=False
                ))
                
                if item_exception or not item_result or not item_result.object_id:
                    continue
                    
                # Convert DOM node to node ID
                node_id = self.execute_command(cdp.dom.request_node(object_id=item_result.object_id))
                
                if node_id == 0:
                    continue
                    
                resolved_node = self.resolve_node(node_id)
                if not resolved_node:
                    continue
                    
                node_info = self.execute_command(cdp.dom.describe_node(node_id=node_id))
                
                elements.append(Element(node_id, node_info.backend_node_id, resolved_node.object_id, self))
            
            return elements
        except Exception as e:
            self.logger.error(f"Error in xpath_all: {e}")
            return []

    def get_bounding_box(self, backend_node_id: cdp.dom.BackendNodeId) -> cdp.dom.Rect:
        """Gets the bounding box of an element by its backend node ID."""
        model: cdp.dom.BoxModel = self.execute_command(cdp.dom.get_box_model(backend_node_id=backend_node_id))
        if not model:
            raise ValueError("Could not get box model for element.")

        quads = model.content
        # Extract x and y coordinates from quads
        x_coords = quads[::2]
        y_coords = quads[1::2]

        # Find min and max coordinates to form the bounding box
        min_x = min(x_coords)
        min_y = min(y_coords)
        max_x = max(x_coords)
        max_y = max(y_coords)

        # Calculate width and height
        width = max_x - min_x
        height = max_y - min_y

        return cdp.dom.Rect(x=min_x, y=min_y, width=width, height=height)

    def screenshot(self,
                        path: str,
                        format: str = "png",
                        quality: Optional[int] = None,
                        clip: Optional[cdp.page.Viewport] = None,
                        from_surface: bool = True,
                        capture_beyond_viewport: bool = True,
                        optimize_for_speed: bool = False
                        ) -> bytes:
        """
        Takes a screenshot of the current page.

        Args:
            format: Image format ("png" or "jpeg").
            quality: Compression quality (0-100, for jpeg only).
            clip: Viewport to capture (optional).
            from_surface: Capture from the surface, rather than the view.
            capture_beyond_viewport: Capture content outside the viewport.
            optimize_for_speed: Optimize image encoding for speed.

        Returns:
            The screenshot data as bytes.
        """
        if format not in ("png", "jpeg"):
            raise ValueError("Invalid image format.  Must be 'png' or 'jpeg'.")
        if quality is not None and format != "jpeg":
            raise ValueError("Quality parameter only applies to JPEG format.")
        if quality is not None and (quality < 0 or quality > 100):
            raise ValueError("Quality must be between 0 and 100.")

        data = self.execute_command(cdp.page.capture_screenshot(
            format_=format,
            quality=quality,
            clip=clip,
            from_surface=from_surface,
            capture_beyond_viewport=capture_beyond_viewport,
            optimize_for_speed=optimize_for_speed
        ))

        if not data:
            raise ValueError("Could not capture screenshot of element.")

        data_bytes = base64.b64decode(data)
        with open(path, "wb") as file:
            file.write(data_bytes)
        
        return data_bytes

   

    def scroll_by(self, x: int = 0, y: int = 0,
                  x_distance: int = 0, y_distance: int = 0,
                    wait_time: float = 0.5) -> None:
        """
        Scrolls the page by the specified amount.
        
        Args:
            x: X coordinate of the start of the gesture in CSS pixels..
            y: Y coordinate of the start of the gesture in CSS pixels..
            x_distance: (Optional) The distance to scroll along the X axis (positive to scroll left).
            y_distance: (Optional) The distance to scroll along the Y axis (positive to scroll up).
            wait_time: Time to wait after scrolling (in seconds).
        """
        self.logger.info(f"Scrolling by x={x}, y={y}")
        
        # Execute JavaScript to scroll by the specified amount
        self.execute_command(
            cdp.input_.synthesize_scroll_gesture(
                    x=x,
                    y=y,
                    y_distance=y_distance,
                    x_distance=x_distance
            )
        )
        
        
        # Wait for scrolling to complete
        if wait_time > 0:
            self._wait(wait_time)

    def print_to_pdf(self, options = None) -> bytes:
        """
        Prints the current page to PDF.

        Args:
            options: PDF printing options.

        Returns:
            The PDF data as bytes.
        """
        data , stream  = self.execute_command(cdp.page.print_to_pdf(**(options.to_dict() if options else {})))
        return bytes.fromhex(data)  # type: ignore

    def handle_javascript_dialog(self, accept: bool, prompt_text: str = "") -> None:
        """
        Handles a JavaScript dialog (alert, confirm, prompt).

        Args:
            accept: Whether to accept or dismiss the dialog.
            prompt_text: Text to enter in a prompt dialog (optional).
        """
        self.execute_command(cdp.page.handle_java_script_dialog(accept=accept, prompt_text=prompt_text))

    def enable_dom(self) -> None:
        """Enables DOM events."""
        if self._dom_enable_count == 0:
            self.execute_command(cdp.dom.enable())
        self._dom_enable_count += 1

    def disable_dom(self) -> None:
        """Disables DOM events."""
        if self._dom_enable_count > 0:
            self._dom_enable_count -= 1
            if self._dom_enable_count == 0:
                self.execute_command(cdp.dom.disable())

    def enable_page(self) -> None:
        """Enables Page events."""
        if self._page_enable_count == 0:
            self.execute_command(cdp.page.enable())
        self._page_enable_count += 1

    def disable_page(self) -> None:
        """Disables Page events."""
        if self._page_enable_count > 0:
            self._page_enable_count -= 1
            if self._page_enable_count == 0:
                self.execute_command(cdp.page.disable())

    def enable_runtime(self) -> None:
        """Enables Runtime events."""
        if self._runtime_enable_count == 0:
            self.execute_command(cdp.runtime.enable())
        self._runtime_enable_count += 1

    def disable_runtime(self) -> None:
        """Disables Runtime events."""
        if self._runtime_enable_count > 0:
            self._runtime_enable_count -= 1
            if self._runtime_enable_count == 0:
                self.execute_command(cdp.runtime.disable())

    def get_url(self) -> str:
        """Gets the current URL of the tab."""
        # Use Javascript to get the URL, as there's no direct CDP command
        result, exception_logs = self.execute_command(cdp.runtime.evaluate(expression="window.location.href", return_by_value=True))
        return result.value # type: ignore

    def close(self) -> None:
        """Closes the tab."""
        try:
            pass
            #self.execute_command(cdp.browser.close())  # Close the entire browser.
            # Ideally, we would use Target.closeTarget, but it requires the
            # initial CDPController to send the command, not the Tab instance.
        except Exception as e:
            self.logger.error(f"Error closing tab: {e}")
        finally:
            self.disconnect()

    def __enter__(self) -> 'Tab':
        self.connect()
        self.enable_page()
        self.enable_dom()
        self.enable_runtime()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.disable_runtime()
        self.disable_dom()
        self.disable_page()
        self.disconnect()