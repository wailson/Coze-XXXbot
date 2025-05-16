import asyncio
import json
import re
import traceback

import aiohttp
from loguru import logger
import tomllib
import os

from WechatAPI import WechatAPIClient
from database.XYBotDB import XYBotDB
from utils.decorators import *
from utils.plugin_base import PluginBase



class Coze(PluginBase):
    description = "Coze_Simple插件"
    author = "wilson"
    version = "1.0.0"
    is_ai_platform = True  # 标记为 AI 平台插件

    def __init__(self):
        super().__init__()


        try:
            with open("plugins/Coze/config.toml", "rb") as f:
                config = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            logger.error(f"配置文件解析失败: {e}")
            raise ValueError("请检查 plugins/Coze/config.toml 文件的格式是否正确")
        except FileNotFoundError:
            logger.error("配置文件未找到")
            raise FileNotFoundError("请确保 plugins/Coze/config.toml 文件存在")

        # 读取基本配置
        coze_config = config["Coze"]
        self.enable = coze_config["enable"]  # 读取插件开关
        self.api_key = coze_config["api-key"]
        self.base_url = coze_config["base-url"]

        self.commands = coze_config["commands"] #唤醒AI
        self.command_tip = coze_config["command-tip"]
        self.bot_id = coze_config["bot_id"] #模型体ID

        self.db = XYBotDB() #暂时不清楚保存在哪里

    @on_text_message(priority=20)
    async def handle_text(self, bot: WechatAPIClient, message: dict):
        """处理文本消息"""
        if not self.enable:
            return True # 插件未启用，允许后续插件处理

        content = str(message["Content"]).strip()
        command = content.split(" ", 1)
        is_command = len(command) > 0 and command[0] in self.commands

        # 修改这部分逻辑，私聊时不需要触发命令
        if message["IsGroup"]:
            # 群聊需要触发命令
            if not is_command:  # 不是指令，且是群聊
                return True
            elif len(command) == 1 and is_command:  # 只是指令，但没请求内容
                await bot.send_at_message(message["FromWxid"], "\n" + self.command_tip, [message["SenderWxid"]])
                return True
        else:
            # 私聊时直接响应，不需要触发命令
            # 但如果仅仅输入的是命令，也显示提示
            if len(command) == 1 and is_command:
                await bot.send_text_message(message["FromWxid"], self.command_tip)
                return True
            # 记录处理日志，确认私聊消息被处理
            logger.info(f"处理私聊消息_Coze: {content}")

        if not self.api_key:
            if message["IsGroup"]:
                await bot.send_at_message(message["FromWxid"], "\n你还没配置Coze API密钥！", [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], "你还没配置Coze API密钥！")
            return False


        await self.coze(bot, message, content)
        return False

    @on_at_message(priority=20)
    async def handle_at(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return True

        if not self.api_key:
            await bot.send_at_message(message["FromWxid"], "\n你还没配置Coze API密钥！", [message["SenderWxid"]])
            return False


        await self.coze(bot, message, message["Content"])

        return False

    async def coze(self, bot: WechatAPIClient, message: dict, query: str, files=None):
        # 构建post请求体
        if files is None:
            files = []
        conversation_id = self.db.get_llm_thread_id(message["FromWxid"], namespace="coze")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream"
        }
        payload = json.dumps({
            "bot_id": self.bot_id,
            "user_id": message["FromWxid"],
            "stream": True,
            "content_type": "object_string",
            "auto_save_history": True,
            "additional_messages": [{
                "role": "user",
                "content": query,
                "content_type": "text"
            }]
        })

        url = f"{self.base_url}"
        ai_resp = ""
        new_con_id = conversation_id  # 初始化为当前会话ID
        current_chat_id = ""  # 跟踪当前chat_id
        buffer = ""

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                async with session.post(url=url, headers=headers, data=payload) as resp:
                    if resp.status != 200:
                        error_msg = await resp.text()
                        logger.error(f"API请求失败: 状态码={resp.status}, 错误={error_msg}")
                        if resp.status == 404:
                            self.db.save_llm_thread_id(message["FromWxid"], "", "coze")
                            return await self.coze(bot, message, query)
                        elif resp.status == 400:
                            return await self.handle_400(bot, message, resp)
                        elif resp.status == 500:
                            return await self.handle_500(bot, message)
                        else:
                            return await self.handle_other_status(bot, message, resp)

                    # 处理SSE流
                    event_type = None
                    async for raw_line in resp.content:
                        try:
                            line = raw_line.decode('utf-8').strip()

                            # 跳过空行和ping事件
                            if not line or line == "event: ping":
                                continue

                            # 处理事件类型行
                            if line.startswith("event:"):
                                event_type = line[6:].strip()
                                continue

                            # 处理数据行
                            if line.startswith("data:"):
                                data = line[5:].strip()

                                # 处理[DONE]信号
                                if data == '"[DONE]"':
                                    logger.debug("收到流结束信号")
                                    break

                                try:
                                    event_data = json.loads(data)
                                except json.JSONDecodeError as e:
                                    logger.warning(f"JSON解析失败: {e}\n原始数据: {data[:200]}...")
                                    continue

                                logger.debug(
                                    f"处理事件: {event_type} | 数据: {json.dumps(event_data, ensure_ascii=False)}")

                                # 处理不同事件类型
                                if event_type == "conversation.chat.created":
                                    current_chat_id = event_data.get("id", "")
                                    new_con_id = event_data.get("conversation_id", new_con_id)
                                    logger.info(f"新会话创建: chat_id={current_chat_id}, conversation_id={new_con_id}")

                                elif event_type == "conversation.message.delta":
                                    if event_data.get("type") == "answer":
                                        delta = event_data.get("content", "")
                                        ai_resp += delta
                                        logger.debug(f"收到回答片段: {delta}")

                                elif event_type == "conversation.message.completed":
                                    if event_data.get("type") == "answer":
                                        completed = event_data.get("content", "")
                                        if completed:
                                            ai_resp = completed  # 覆盖增量内容
                                        logger.info(f"收到完整回答: {completed}")

                                elif event_type == "conversation.chat.completed":
                                    usage = event_data.get("usage", {})
                                    logger.info(f"会话完成: 消耗token={usage.get('token_count', 0)}")

                                elif event_type == "conversation.message.error":
                                    await self.coze_handle_error(
                                        bot, message,
                                        event_data.get("code", "UNKNOWN"),
                                        event_data.get("msg", "未知错误")
                                    )
                                    return False

                        except Exception as e:
                            logger.error(f"处理流数据时出错: {str(e)}", exc_info=True)
                            continue

                    # 保存新的会话ID
                    if new_con_id and new_con_id != conversation_id:
                        try:
                            self.db.save_llm_thread_id(message["FromWxid"], new_con_id, "coze")
                            logger.info(f"更新会话ID: {conversation_id} -> {new_con_id}")
                        except Exception as e:
                            logger.error(f"保存会话ID失败: {str(e)}")

                    # 返回最终回复
                    if ai_resp:
                        await self.coze_handle_text(bot, message, ai_resp)
                        return True
                    return False

        except asyncio.TimeoutError:
            logger.error("请求超时")
            await self.coze_handle_error(bot, message, "TIMEOUT", "请求超时")
            return False
        except aiohttp.ClientError as e:
            logger.error(f"网络请求失败: {str(e)}")
            await self.coze_handle_error(bot, message, "NETWORK_ERROR", str(e))
            return False
        except Exception as e:
            logger.critical(f"未处理的异常: {str(e)}", exc_info=True)
            await self.coze_handle_error(bot, message, "SYSTEM_ERROR", str(e))
            return False

    async def coze_handle_text(self, bot: WechatAPIClient, message: dict, text: str):
        # 清理文本中的链接标记
        pattern = r'\[[^\]]+\]\(https?:\/\/[^\s\)]+\)'
        text = re.sub(pattern, '', text)

        # 清理文本末尾的多余空格和换行符
        text = text.rstrip()

        if text:
            # 判断是否为群聊
            is_group = message.get("IsGroup", False)
            if is_group:
                # 群聊消息添加换行作为分隔
                await bot.send_at_message(message["FromWxid"], "\n" + text, [message["SenderWxid"]])
            else:
                # 私聊直接发送文本
                await bot.send_text_message(message["FromWxid"], text)

    @staticmethod
    async def coze_handle_error(bot: WechatAPIClient, message: dict, code: int, err_message: str):
        output = ("-----XYBot-----\n"
                  "🙅对不起，Coze出现错误！\n"
                  f"错误码：{code}\n"
                  f"错误信息：{err_message}")
        await bot.send_at_message(message["FromWxid"], "\n" + output, [message["SenderWxid"]])

    @staticmethod
    async def handle_400(bot: WechatAPIClient, message: dict, resp: aiohttp.ClientResponse):
        output = ("-----XYBot-----\n"
                  "🙅对不起，出现错误！\n"
                  f"错误信息：{(await resp.content.read()).decode('utf-8')}")
        await bot.send_at_message(message["FromWxid"], "\n" + output, [message["SenderWxid"]])

    @staticmethod
    async def handle_500(bot: WechatAPIClient, message: dict):
        output = "-----XYBot-----\n🙅对不起，Coze服务内部异常，请稍后再试。"
        await bot.send_at_message(message["FromWxid"], "\n" + output, [message["SenderWxid"]])

    @staticmethod
    async def handle_other_status(bot: WechatAPIClient, message: dict, resp: aiohttp.ClientResponse):
        ai_resp = ("-----XYBot-----\n"
                   f"🙅对不起，出现错误！\n"
                   f"状态码：{resp.status}\n"
                   f"错误信息：{(await resp.content.read()).decode('utf-8')}")
        await bot.send_at_message(message["FromWxid"], "\n" + ai_resp, [message["SenderWxid"]])

