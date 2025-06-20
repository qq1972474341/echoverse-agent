import json
import time
import traceback
from collections.abc import Generator
from copy import deepcopy
from typing import Any, Optional, cast

from utils.mcp_client import create_mcp_client

from dify_plugin.entities.agent import AgentInvokeMessage
from dify_plugin.entities.model import ModelFeature
from dify_plugin.entities.model.llm import (
    LLMModelConfig,
    LLMResult,
    LLMResultChunk,
    LLMUsage,
)
from dify_plugin.entities.model.message import (
    AssistantPromptMessage,
    PromptMessage,
    PromptMessageContentType,
    SystemPromptMessage,
    ToolPromptMessage,
    UserPromptMessage,
    PromptMessageTool
)
from dify_plugin.entities.tool import LogMetadata, ToolInvokeMessage, ToolProviderType
from dify_plugin.interfaces.agent import (
    AgentModelConfig,
    AgentStrategy,
    ToolEntity,
    ToolInvokeMeta,
)
from pydantic import BaseModel

api_host = "www.label-studio.top"
# 控制是否输出调试信息的开关
DEBUG_MODE = True

def debug_print(*args, **kwargs):
    """
    根据DEBUG_MODE的值决定是否打印调试信息
    """
    if DEBUG_MODE:
        print(*args, **kwargs)

class FunctionCallingParams(BaseModel):
    api_key: str
    api_host: str = api_host  # 新增api_host参数，默认取全局变量
    device_id: str
    query: str
    instruction: str | None
    model: AgentModelConfig
    tools: list[ToolEntity] | None 
    maximum_iterations: int = 3
    

class PromptMessageMcpTool(PromptMessageTool):
    tool_type: str = "mcp"  # 标识这是MCP工具

class FunctionCallingAgentStrategy(AgentStrategy):
    def __init__(self, session):
        super().__init__(session)
        self.query = ""
        self.instruction = ""

    @property
    def _user_prompt_message(self) -> UserPromptMessage:
        return UserPromptMessage(content=self.query)

    @property
    def _system_prompt_message(self) -> SystemPromptMessage:
        return SystemPromptMessage(content=self.instruction)

    def _invoke(self, parameters: dict[str, Any]) -> Generator[AgentInvokeMessage]:
        """
        Run FunctionCall agent application
        """
        # 打印一条分割线
        debug_print("========================================")

        fc_params = FunctionCallingParams(**parameters)
        # 优先使用参数中的api_host，否则用全局变量
        api_host_val = getattr(fc_params, 'api_host', None) or api_host
           
        # init prompt messages
        api_key = fc_params.api_key  # 这里的api_key是从参数中获取的
        device_id = fc_params.device_id # 设备id

        query = fc_params.query
        self.query = query
        self.instruction = fc_params.instruction # 这里的instruction是从参数中获取的 指令提示词
        history_prompt_messages = fc_params.model.history_prompt_messages
        history_prompt_messages.insert(0, self._system_prompt_message)
        history_prompt_messages.append(self._user_prompt_message)

        debug_print(f"api_key: {api_key}")

        # convert tool messages
        tools = fc_params.tools

        tool_instances = {tool.identity.name: tool for tool in tools} if tools else {}
        
        # 初始化工具
        # 这里的工具实例是从工具列表中获取的 PromptMessageTool
        prompt_messages_tools = self._init_prompt_tools(tools)

        # Dynamically fetch MCP tools from API only if api_key is present
        mcp_client = create_mcp_client(api_host_val, api_key, device_id)
        if mcp_client:
            # Fetch MCP tools from API
            debug_print(f"Fetching MCP tools from MCP API: {mcp_client.base_url}")
            debug_print(f"Request headers: X-API-Key: {api_key}, X-Device-ID: {device_id}")
            
            mcp_tools_data = mcp_client.fetch_mcp_tools()

            if mcp_tools_data.get("code") == 1000 and "data" in mcp_tools_data:
                data = mcp_tools_data["data"]
                tools_list = data.get("tools", []) if isinstance(data, dict) else []
                debug_print(f"Successfully fetched {data.get('count', len(tools_list))} MCP tools from API")
                
                # 用于去重的工具名称集合
                existing_tool_names = {tool.name for tool in prompt_messages_tools}
                
                for tool_data in tools_list:
                    try:
                        # 处理MCP工具格式
                        tool_name = tool_data.get("name")
                        tool_description = tool_data.get("description")
                        input_schema = tool_data.get("inputSchema", {})

                        # 更严格的字段验证
                        if not tool_name or not isinstance(tool_name, str):
                            debug_print(f"Warning: Skipping MCP tool due to invalid name. Raw tool_data: {tool_data}")
                            continue
                        
                        if not tool_description or not isinstance(tool_description, str):
                            debug_print(f"Warning: Skipping MCP tool due to invalid description. Raw tool_data: {tool_data}")
                            continue
                        
                        # 检查工具是否已存在，避免重复添加
                        if tool_name in existing_tool_names:
                            debug_print(f"Warning: Skipping duplicate MCP tool: {tool_name}")
                            continue
                        
                        # 将inputSchema转换为parameters格式，确保是dict类型
                        if isinstance(input_schema, dict):
                            tool_parameters = input_schema
                        else:
                            debug_print(f"Warning: Invalid inputSchema for tool {tool_name}, using empty dict")
                            tool_parameters = {}

                        # 转换parameters中的title字段为description字段
                        if tool_parameters and isinstance(tool_parameters, dict):
                            properties = tool_parameters.get('properties', {})
                            if isinstance(properties, dict):
                                for prop_name, prop_config in properties.items():
                                    if isinstance(prop_config, dict) and 'title' in prop_config:
                                        # 将title字段转换为description字段
                                        prop_config['description'] = prop_config.pop('title')
                                        debug_print(f"Converted 'title' to 'description' for property {prop_name} in tool {tool_name}")

                        # 确保描述字段格式正确
                        tool_description = str(tool_description).strip()
                        if not tool_description:
                            tool_description = f"MCP tool: {tool_name}"
                            debug_print(f"Warning: Empty description for tool {tool_name}, using default")

                        # 验证参数结构
                        if tool_parameters and not isinstance(tool_parameters, dict):
                            debug_print(f"Warning: Invalid parameters structure for tool {tool_name}, resetting to empty")
                            tool_parameters = {}

                        debug_print(f"Creating MCP tool - Name: {tool_name}, Description: {tool_description}, Parameters: {tool_parameters}")

                        mcp_tool = PromptMessageMcpTool(
                            name=str(tool_name),
                            description=str(tool_description),
                            parameters=tool_parameters,
                        )
                        
                        # 验证创建的工具对象
                        if not hasattr(mcp_tool, 'name') or not hasattr(mcp_tool, 'description'):
                            debug_print(f"Error: Created tool object is missing required attributes for {tool_name}")
                            continue
                        
                        prompt_messages_tools.append(mcp_tool)
                        existing_tool_names.add(tool_name)
                        debug_print(f"Successfully added MCP tool: {tool_name}")
                        
                    except Exception as e:
                        debug_print(f"Error processing individual MCP tool data: {tool_data}. Error: {e}")
                        import traceback
                        debug_print(f"Full traceback: {traceback.format_exc()}")
            elif mcp_tools_data.get("code") == 1001:
                debug_print(f"Error fetching MCP tools: API key invalid or other API error. Message: {mcp_tools_data.get('message')}")
            elif mcp_tools_data.get("code") == -1:
                debug_print(f"Error fetching MCP tools: {mcp_tools_data.get('message')} (Type: {mcp_tools_data.get('error_type')})")
            else:
                debug_print(f"Error fetching MCP tools: {mcp_tools_data.get('message', 'Unknown API error')}. Response code: {mcp_tools_data.get('code')}")

            # Fetch IoT device states from API (commented out as per original code)
            # debug_print(f"Fetching IoT device states from: {mcp_client.base_url}")
            # debug_print(f"Request headers for device states: X-API-Key: {api_key}, X-Device-ID: {device_id}")
            
            # device_states_data = mcp_client.fetch_device_states()

            # if device_states_data.get("code") == 1000:
            #     if "data" in device_states_data and isinstance(device_states_data["data"], list):
            #         device_states_str = json.dumps(device_states_data["data"], ensure_ascii=False)
            #         debug_print(f"Successfully fetched IoT device states. Data (as string): {device_states_str}")
            #         # 将设备状态插入到提示词中，方便用户查询
            #         history_prompt_messages.insert(0, SystemPromptMessage(content=f"当前物联网设备状态的数组JSON如下,请从其中查找设备的状态并且以自然语言描述：{device_states_str}"))
            #     elif "data" in device_states_data:
            #         debug_print(f"IoT device states API success (code 1000), but 'data' field is not a list. Raw 'data': {device_states_data['data']}")
            #     else:
            #         debug_print(f"IoT device states API success (code 1000), but 'data' field is missing. Response: {device_states_data}")
            # elif device_states_data.get("code") == 1001: 
            #     debug_print(f"Error fetching IoT device states: API error (code {device_states_data.get('code')}). Message: {device_states_data.get('message')}")
            # elif device_states_data.get("code") == -1:
            #     debug_print(f"Error fetching IoT device states: {device_states_data.get('message')} (Type: {device_states_data.get('error_type')})")
            # else:
            #     debug_print(f"Error fetching IoT device states: {device_states_data.get('message', 'Unknown API error')}. Response code: {device_states_data.get('code')}")
            
        else:
            debug_print("Skipping fetching MCP tools from API because api_key or device_id is not provided.")

        # init model parameters

        # 判断当前模型是否支持流式工具调用
        stream = (
            ModelFeature.STREAM_TOOL_CALL in fc_params.model.entity.features
            if fc_params.model.entity and fc_params.model.entity.features
            else False
        )
        model = fc_params.model

        # 模型停止序列
        stop = (
            fc_params.model.completion_params.get("stop", [])
            if fc_params.model.completion_params
            else []
        )

        # init function calling state
        iteration_step = 1
        max_iteration_steps = fc_params.maximum_iterations
        current_thoughts: list[PromptMessage] = []
        function_call_state = True  # 运行直到没有工具调用
        llm_usage: dict[str, Optional[LLMUsage]] = {"usage": None}
        final_answer = ""

        while function_call_state and iteration_step <= max_iteration_steps:
            # start a new round
            function_call_state = False
            round_started_at = time.perf_counter()
            round_log = self.create_log_message(
                label=f"ROUND {iteration_step}",
                data={},
                metadata={
                    LogMetadata.STARTED_AT: round_started_at,
                },
                status=ToolInvokeMessage.LogMessage.LogStatus.START,
            )
            yield round_log

            # If max_iteration_steps=1, need to execute tool calls
            if iteration_step == max_iteration_steps and max_iteration_steps > 1:
                # 最后一次迭代，移除所有工具
                prompt_messages_tools = []

            # recalc llm max tokens

            
            prompt_messages = self._organize_prompt_messages(
                history_prompt_messages=history_prompt_messages,
                current_thoughts=current_thoughts,
            )
            if model.entity and model.completion_params:
                # 重新计算模型的最大token数
                self.recalc_llm_max_tokens(
                    model.entity, prompt_messages, model.completion_params
                )

            # 执行模型
            model_started_at = time.perf_counter()
            
            # 添加工具验证调试信息
            debug_print(f"ROUND {iteration_step} - Validating {len(prompt_messages_tools)} tools before sending to model:")
            for i, tool in enumerate(prompt_messages_tools):
                debug_print(f"  Tool {i+1}: name='{tool.name}', description='{tool.description}', parameters={tool.parameters}")
                
                # 验证工具的必需属性
                if not hasattr(tool, 'name') or not tool.name:
                    debug_print(f"  ERROR: Tool {i+1} missing or empty name!")
                if not hasattr(tool, 'description') or not tool.description:
                    debug_print(f"  ERROR: Tool {i+1} missing or empty description!")
                if not hasattr(tool, 'parameters'):
                    debug_print(f"  ERROR: Tool {i+1} missing parameters attribute!")
            
            model_log = self.create_log_message(
                label=f"{model.model} Thought",
                data={},
                metadata={
                    LogMetadata.STARTED_AT: model_started_at,
                    LogMetadata.PROVIDER: model.provider,
                },
                parent=round_log,
                status=ToolInvokeMessage.LogMessage.LogStatus.START,
            )
            yield model_log
            model_config = LLMModelConfig(**model.model_dump(mode="json"))
            chunks: Generator[LLMResultChunk, None, None] | LLMResult = (
                self.session.model.llm.invoke(
                    model_config=model_config,
                    prompt_messages=prompt_messages,
                    stop=stop,
                    stream=stream,
                    tools=prompt_messages_tools,
                )
            )

            tool_calls: list[tuple[str, str, dict[str, Any]]] = []

            # save full response
            response = ""

            # save tool call names and inputs
            tool_call_names = ""

            current_llm_usage = None
            debug_print(f"ROUND {iteration_step} prompt_messages: {prompt_messages}")
            debug_print(f"ROUND {iteration_step} prompt_messages_tools: {prompt_messages_tools}")
            
           
            if isinstance(chunks, Generator):
                for chunk in chunks:
                    # debug_print(f"ROUND {iteration_step} chunk: {chunk}")
                    # 检查是否有工具调用
                    if self.check_tool_calls(chunk):
                        function_call_state = True
                        tool_calls.extend(self.extract_tool_calls(chunk) or [])
                        tool_call_names = ";".join(
                            [tool_call[1] for tool_call in tool_calls]
                        )
                    
                    if chunk.delta.message and chunk.delta.message.content:
                        if isinstance(chunk.delta.message.content, list):
                            for content in chunk.delta.message.content:
                                response += content.data
                                if (
                                    not function_call_state
                                    or iteration_step == max_iteration_steps
                                ):
                                    yield self.create_text_message(content.data)
                        else:
                            response += str(chunk.delta.message.content)
                            if (
                                not function_call_state
                                or iteration_step == max_iteration_steps
                            ):
                                yield self.create_text_message(
                                    str(chunk.delta.message.content)
                                )

                    if chunk.delta.usage:
                        self.increase_usage(llm_usage, chunk.delta.usage)
                        current_llm_usage = chunk.delta.usage
            else:
                result = chunks
                result = cast(LLMResult, result)
                # check if there is any tool call
                if self.check_blocking_tool_calls(result):
                    function_call_state = True
                    tool_calls.extend(self.extract_blocking_tool_calls(result) or [])
                    tool_call_names = ";".join(
                        [tool_call[1] for tool_call in tool_calls]
                    )


                if result.usage:
                    self.increase_usage(llm_usage, result.usage)
                    current_llm_usage = result.usage

                if result.message and result.message.content:
                    if isinstance(result.message.content, list):
                        for content in result.message.content:
                            response += content.data
                    else:
                        response += str(result.message.content)

                if not result.message.content:
                    result.message.content = ""
                if isinstance(result.message.content, str):
                    yield self.create_text_message(result.message.content)
                elif isinstance(result.message.content, list):
                    for content in result.message.content:
                        yield self.create_text_message(content.data)

            yield self.finish_log_message(
                log=model_log,
                data={
                    "output": response,
                    "tool_name": tool_call_names,
                    "tool_input": [
                        {"name": tool_call[1], "args": tool_call[2]}
                        for tool_call in tool_calls
                    ],
                },
                metadata={
                    LogMetadata.STARTED_AT: model_started_at,
                    LogMetadata.FINISHED_AT: time.perf_counter(),
                    LogMetadata.ELAPSED_TIME: time.perf_counter() - model_started_at,
                    LogMetadata.PROVIDER: model.provider,
                    LogMetadata.TOTAL_PRICE: current_llm_usage.total_price
                    if current_llm_usage
                    else 0,
                    LogMetadata.CURRENCY: current_llm_usage.currency
                    if current_llm_usage
                    else "",
                    LogMetadata.TOTAL_TOKENS: current_llm_usage.total_tokens
                    if current_llm_usage
                    else 0,
                },
            )
            assistant_message = AssistantPromptMessage(content="", tool_calls=[])
            if not tool_calls:
                assistant_message.content = response
                current_thoughts.append(assistant_message)

            final_answer += response + "\n"
            debug_print(f"ROUND {iteration_step} final_answer: {final_answer}")
            debug_print(f"ROUND {iteration_step} tool_calls: {tool_calls}")
            # 调用工具
            tool_responses = []
            for tool_call_id, tool_call_name, tool_call_args in tool_calls:
                # 添加工具调用到当前思考中
                current_thoughts.append(
                    AssistantPromptMessage(
                        content="",
                        tool_calls=[
                            AssistantPromptMessage.ToolCall(
                                id=tool_call_id,
                                type="function",
                                function=AssistantPromptMessage.ToolCall.ToolCallFunction(
                                    name=tool_call_name,
                                    arguments=json.dumps(
                                        tool_call_args, ensure_ascii=False
                                    ),
                                ),
                            )
                        ],
                    )
                )
                # 工具实例
                tool_instance = tool_instances.get(tool_call_name)
                tool_call_started_at = time.perf_counter()

                # 记录工具调用日志
                tool_call_log = self.create_log_message(
                    label=f"CALL {tool_call_name}",
                    data={},
                    metadata={
                        LogMetadata.STARTED_AT: time.perf_counter(),
                        LogMetadata.PROVIDER: tool_instance.identity.provider if tool_instance else "unknown",
                    },
                    parent=round_log,
                    status=ToolInvokeMessage.LogMessage.LogStatus.START,
                )
                yield tool_call_log
                if not tool_instance:
                    # 判断是否在prompt_messages_tools中且是McpToolEntity  这里是MCP工具调用
                    mcp_instance = next(
                        (
                            tool
                            for tool in prompt_messages_tools
                            if tool.name == tool_call_name
                            and isinstance(tool, PromptMessageMcpTool)
                        ),
                        None,
                    )

                    if mcp_instance:
                        # 这里是MCP工具调用，使用MCP客户端
                        if not mcp_client:
                            mcp_client = create_mcp_client(api_host_val, api_key, device_id)
                        
                        tool_response_str = 'success'
                        if mcp_client:
                            debug_print(f"Executing MCP tool: {mcp_client.base_url}/open/iot/device/executeMcpTool")
                            debug_print(f"MCP tool name: {tool_call_name}, arguments: {tool_call_args}")
                            debug_print(f"MCP headers: X-API-Key: {api_key}, X-Device-ID: {device_id}")
                            
                            # 调用MCP客户端执行MCP工具
                            mcp_result = mcp_client.execute_mcp_tool(
                                tool_name=tool_call_name,
                                params=tool_call_args
                            )
                            
                            debug_print(f"MCP response: {mcp_result}")
                            
                            if mcp_result.get("code") == 1000:
                                # 如果API返回成功，使用返回的数据作为工具响应
                                if "data" in mcp_result:
                                    tool_response_str = json.dumps(mcp_result["data"], ensure_ascii=False)
                                else:
                                    tool_response_str = "success"
                            elif mcp_result.get("code") == -1:
                                tool_response_str = mcp_result.get("message", "Unknown error")
                            else:
                                error_message = mcp_result.get("message", "Unknown error")
                                tool_response_str = f"Failed to execute MCP tool: {error_message}"
                        else:
                            tool_response_str = "Failed to create MCP client for MCP tool execution"
                        
                        # 添加工具响应到当前思考中
                        current_thoughts.append(
                        ToolPromptMessage(
                            content=str(tool_response_str),  # 工具响应
                            tool_call_id=tool_call_id,
                            name=tool_call_name,
                            )
                        )
                        tool_response = {
                            "tool_call_id": tool_call_id,
                            "tool_call_name": tool_call_name,
                            "tool_call_input": {
                                # **tool_instance.runtime_parameters,
                                **tool_call_args,
                            },
                            "tool_response": tool_response_str,
                        }
                    else:
                        tool_response = {
                            "tool_call_id": tool_call_id,
                            "tool_call_name": tool_call_name,
                            "tool_response": f"there is not a tool named {tool_call_name}",
                            "meta": ToolInvokeMeta.error_instance(
                                f"there is not a tool named {tool_call_name}"
                            ).to_dict(),
                        }
                        
                else:
                    # 执行工具
                    try:
                        tool_invoke_responses = self.session.tool.invoke(
                            provider_type=ToolProviderType(tool_instance.provider_type),
                            provider=tool_instance.identity.provider,
                            tool_name=tool_instance.identity.name,
                            parameters={
                                **tool_instance.runtime_parameters,
                                **tool_call_args,
                            },
                        )
                        result = ""
                        # 处理工具调用的响应
                        for response in tool_invoke_responses:
                            if response.type == ToolInvokeMessage.MessageType.TEXT:
                                result += cast(
                                    ToolInvokeMessage.TextMessage, response.message
                                ).text
                            elif response.type == ToolInvokeMessage.MessageType.LINK:
                                result += (
                                    f"result link: {cast(ToolInvokeMessage.TextMessage, response.message).text}."
                                    + " please tell user to check it."
                                )
                            elif response.type in {
                                ToolInvokeMessage.MessageType.IMAGE_LINK,
                                ToolInvokeMessage.MessageType.IMAGE,
                            }:
                                result += (
                                    "image has been created and sent to user already, "
                                    + "you do not need to create it, just tell the user to check it now."
                                )
                            elif response.type == ToolInvokeMessage.MessageType.JSON:
                                text = json.dumps(
                                    cast(
                                        ToolInvokeMessage.JsonMessage, response.message
                                    ).json_object,
                                    ensure_ascii=False,
                                )
                                result += f"tool response: {text}."
                            else:
                                result += f"tool response: {response.message!r}."
                    except Exception as e:
                        result = f"tool invoke error: {e!s}"
                    tool_response = {
                        "tool_call_id": tool_call_id,
                        "tool_call_name": tool_call_name,
                        "tool_call_input": {
                            **tool_instance.runtime_parameters,
                            **tool_call_args,
                        },
                        "tool_response": result,
                    }
                    debug_print(f"ROUND {iteration_step} runtime_parameters: {tool_instance.runtime_parameters}")
                    debug_print(f"ROUND {iteration_step} tool_call_args: {tool_call_args}")

                yield self.finish_log_message(
                    log=tool_call_log,
                    data={
                        "output": tool_response,
                    },
                    metadata={
                        LogMetadata.STARTED_AT: tool_call_started_at,
                        LogMetadata.PROVIDER: tool_instance.identity.provider if tool_instance else "unknown",
                        LogMetadata.FINISHED_AT: time.perf_counter(),
                        LogMetadata.ELAPSED_TIME: time.perf_counter()
                        - tool_call_started_at,
                    },
                )
                tool_responses.append(tool_response)
                if tool_response["tool_response"] is not None:
                    # 添加工具响应到当前思考中
                    current_thoughts.append(
                        ToolPromptMessage(
                            content=str(tool_response["tool_response"]),
                            tool_call_id=tool_call_id,
                            name=tool_call_name,
                        )
                    )

            # 更新工具提示词 (暂时没发现作用)
            for prompt_tool in prompt_messages_tools:
                if tool_instances.get(prompt_tool.name):
                    self.update_prompt_message_tool(
                        tool_instances[prompt_tool.name], prompt_tool
                    )
            debug_print(f"ROUND {iteration_step} updated prompt_messages_tools: {prompt_messages_tools}")
            yield self.finish_log_message(
                log=round_log,
                data={
                    "output": {
                        "llm_response": response,
                        "tool_responses": tool_responses,
                    },
                },
                metadata={
                    LogMetadata.STARTED_AT: round_started_at,
                    LogMetadata.FINISHED_AT: time.perf_counter(),
                    LogMetadata.ELAPSED_TIME: time.perf_counter() - round_started_at,
                    LogMetadata.TOTAL_PRICE: current_llm_usage.total_price
                    if current_llm_usage
                    else 0,
                    LogMetadata.CURRENCY: current_llm_usage.currency
                    if current_llm_usage
                    else "",
                    LogMetadata.TOTAL_TOKENS: current_llm_usage.total_tokens
                    if current_llm_usage
                    else 0,
                },
            )

            # 如果迭代次数为1，则直接返回工具响应
            if tool_responses and max_iteration_steps == 1:
                for resp in tool_responses:
                    yield self.create_text_message(resp["tool_response"])
            iteration_step += 1

        # 返回执行消耗
        yield self.create_json_message(
            {
                "execution_metadata": {
                    LogMetadata.TOTAL_PRICE: llm_usage["usage"].total_price
                    if llm_usage["usage"] is not None
                    else 0,
                    LogMetadata.CURRENCY: llm_usage["usage"].currency
                    if llm_usage["usage"] is not None
                    else "",
                    LogMetadata.TOTAL_TOKENS: llm_usage["usage"].total_tokens
                    if llm_usage["usage"] is not None
                    else 0,
                }
            }
        )

    def check_tool_calls(self, llm_result_chunk: LLMResultChunk) -> bool:
        """
        Check if there is any tool call in llm result chunk
        """
        return bool(llm_result_chunk.delta.message.tool_calls)

    def check_blocking_tool_calls(self, llm_result: LLMResult) -> bool:
        """
        Check if there is any blocking tool call in llm result
        """
        return bool(llm_result.message.tool_calls)

    def extract_tool_calls(
        self, llm_result_chunk: LLMResultChunk
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """
        Extract tool calls from llm result chunk

        Returns:
            List[Tuple[str, str, Dict[str, Any]]]: [(tool_call_id, tool_call_name, tool_call_args)]
        """
        tool_calls = []
        for prompt_message in llm_result_chunk.delta.message.tool_calls:
            args = {}
            if prompt_message.function.arguments != "":
                args = json.loads(prompt_message.function.arguments)

            tool_calls.append(
                (
                    prompt_message.id,
                    prompt_message.function.name,
                    args,
                )
            )

        return tool_calls

    def extract_blocking_tool_calls(
        self, llm_result: LLMResult
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """
        Extract blocking tool calls from llm result

        Returns:
            List[Tuple[str, str, Dict[str, Any]]]: [(tool_call_id, tool_call_name, tool_call_args)]
        """
        tool_calls = []
        for prompt_message in llm_result.message.tool_calls:
            args = {}
            if prompt_message.function.arguments != "":
                args = json.loads(prompt_message.function.arguments)

            tool_calls.append(
                (
                    prompt_message.id,
                    prompt_message.function.name,
                    args,
                )
            )

        return tool_calls

    def _init_system_message(
        self, prompt_template: str, prompt_messages: list[PromptMessage]
    ) -> list[PromptMessage]:
        """
        Initialize system message
        """
        if not prompt_messages and prompt_template:
            return [
                SystemPromptMessage(content=prompt_template),
            ]

        if (
            prompt_messages
            and not isinstance(prompt_messages[0], SystemPromptMessage)
            and prompt_template
        ):
            prompt_messages.insert(0, SystemPromptMessage(content=prompt_template))

        return prompt_messages or []

    def _clear_user_prompt_image_messages(
        self, prompt_messages: list[PromptMessage]
    ) -> list[PromptMessage]:
        """
        这个方法用于清理和转换用户消息中的多模态内容（图片、文件等），将其转换为纯文本形式。
        """
        prompt_messages = deepcopy(prompt_messages)

        for prompt_message in prompt_messages:
            if isinstance(prompt_message, UserPromptMessage) and isinstance(
                prompt_message.content, list
            ):
                prompt_message.content = "\n".join(
                    [
                        content.data
                        if content.type == PromptMessageContentType.TEXT
                        else "[image]"
                        if content.type == PromptMessageContentType.IMAGE
                        else "[file]"
                        for content in prompt_message.content
                    ]
                )

        return prompt_messages

    def _organize_prompt_messages(
        self,
        current_thoughts: list[PromptMessage],
        history_prompt_messages: list[PromptMessage],
    ) -> list[PromptMessage]:
        prompt_messages = [
            *history_prompt_messages,
            *current_thoughts,
        ]
        if len(current_thoughts) != 0:
            # 在第一轮迭代后，清理和转换用户消息中的多模态内容
            prompt_messages = self._clear_user_prompt_image_messages(prompt_messages)
        return prompt_messages
