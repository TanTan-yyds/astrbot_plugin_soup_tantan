# astrbot_plugin_soup_tantan/main.py
"""
海龟汤推理游戏插件 —— 支持汤品分类与篇幅选择，通过外部API或本地fallback获取谜题。
"""
import re
import random
from typing import Dict, Optional, Tuple

import httpx

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import LLMResponse
from astrbot.api import logger, AstrBotConfig
from astrbot.core.utils.session_waiter import (
    session_waiter,
    SessionController,
    SessionFilter,
)
from astrbot.api.message_components import At


# 类型与篇幅映射（与后端 API 数值对齐）
TYPE_MAP = {
    "清汤": 0,
    "甜汤": 1,
    "红汤": 2,
    "黑汤": 3,
}
LENGTH_MAP = {
    "短": 0,
    "中": 1,
    "长": 2,
}

FALLBACK_STORY = {
    "story": "从前有个人，他走进一家酒吧，然后死了。",
    "answer": "他走进的是一家枪械酒吧，被人开枪打死了。",
}


class GroupSessionFilter(SessionFilter):
    """按群 ID 隔离会话"""
    def __init__(self, group_id: str):
        self.group_id = group_id

    def filter(self, event: AstrMessageEvent) -> str:
        current_group = event.get_group_id() or event.unified_msg_origin
        return self.group_id if current_group == self.group_id else ""


class GameState:
    """游戏状态管理"""
    def __init__(self):
        self.active_games: Dict[str, Dict] = {}

    def start_game(self, group_id: str, puzzle: str, answer: str) -> bool:
        if group_id in self.active_games:
            return False
        self.active_games[group_id] = {
            "puzzle": puzzle,
            "answer": answer,
            "is_active": True,
            "qa_history": [],
            "hint_history": [],
        }
        return True

    def end_game(self, group_id: str) -> bool:
        return self.active_games.pop(group_id, None) is not None

    def get_game(self, group_id: str) -> Optional[Dict]:
        return self.active_games.get(group_id)

    def is_game_active(self, group_id: str) -> bool:
        return group_id in self.active_games


@register(
    "astrbot_plugin_soup_tantan",
    "TanTanMao",
    "海龟汤推理游戏插件——支持汤品分类（清汤/红汤/黑汤/甜汤）与篇幅选择（长/中/短）",
    "1.0.0",
    "https://github.com/TanTan-yyds/astrbot_plugin_soup_tantan",
)
class SoupTantanPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        
        # 从框架自动加载的配置中读取
        self.api_url = config.get("api_url", "")
        self.api_token = config.get("api_token", "")
        self.judge_llm_provider = config.get("judge_llm_provider", "")
        self.game_timeout = config.get("game_timeout", 300)
        
        self.game_state = GameState()
        logger.info(f"海龟汤插件初始化完成。API: {self.api_url if self.api_url else '未配置(使用本地测试题)'}")

    # ---------- 帮助 ----------
    def _help_text(self) -> str:
        return (
            "🎮 海龟汤 帮助菜单\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📖 规则：根据汤面提问，推理出完整故事（汤底）。\n"
            "🕹️ 指令列表：\n"
            "  /汤 <清汤/红汤/黑汤/甜汤> <长/中/短>  开始游戏\n"
            "  /汤 help  显示本菜单\n"
            "  /汤状态  查看当前进度\n"
            "  /验证 <推理>  提交最终推理\n"
            "  /揭晓  立即结束并显示答案\n"
            "  /提示  获取一条方向性提示\n"
            "  /查看  查看所有提问记录\n"
            "  /提问+<内容>  向汤面提问（等同于 @bot，必须使用加号连接）\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💡 直接 @bot 或使用 /提问+问题 进行提问，必须加号连接，无空格。\n"
            "💡 /汤 无参数将随机类型与篇幅。\n"
            "💡 无提问和提示次数限制，尽情推理吧！"
        )

    # ---------- API 调用 ----------
    async def _call_soup_api(self, soup_type: int = None, length_type: int = None) -> Optional[Tuple[str, str]]:
        if not self.api_url:
            logger.warning("API地址未配置，将使用本地测试题目")
            return None

        if soup_type is None:
            soup_type = random.choice(list(TYPE_MAP.values()))
        if length_type is None:
            length_type = random.choice(list(LENGTH_MAP.values()))

        headers = {}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"

        # 清洗 self.api_url，提取 base（兼容结尾带 /get 或斜线的情况）
        base_url = re.sub(r'/get/?$', '', self.api_url)
        base_url = re.sub(r'/?$', '', base_url)
        get_url = f"{base_url}/get"

        params = {"soup_type": soup_type, "length_type": length_type}

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # 第一步：GET 获取随机海龟汤
                resp = await client.get(get_url, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") == 0 and "data" in data:
                    story = data["data"].get("story", "")
                    soup_id = data["data"].get("id")

                    # 第二步：POST 获取汤底答案
                    answer_url = f"{base_url}/answer/{soup_id}"
                    ans_resp = await client.post(answer_url, headers=headers)
                    ans_resp.raise_for_status()
                    ans_data = ans_resp.json()

                    answer = ""
                    if ans_data.get("code") == 0 and "data" in ans_data:
                        answer = ans_data["data"].get("answer", "")

                    if story and answer:
                        logger.info(f"API成功，汤面：{story[:20]}...")
                        return story, answer
                logger.warning(f"API返回格式异常: {data}")
        except Exception as e:
            logger.error(f"API请求失败: {e}")
        return None

    def _fallback_story(self) -> Tuple[str, str]:
        return FALLBACK_STORY["story"], FALLBACK_STORY["answer"]

    # ---------- 开始游戏 ----------
    @filter.command("汤")
    async def start_game(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("海龟汤游戏只能在群聊中进行哦~")
            return

        args = event.message_str.strip().split()[1:]
        if args and args[0].lower() in ("help", "帮助"):
            yield event.plain_result(self._help_text())
            return

        if self.game_state.is_game_active(group_id):
            yield event.plain_result("当前群聊已有活跃的海龟汤游戏，请等待结束后再开新局。")
            return

        soup_type = None
        length_type = None
        for a in args:
            if a in TYPE_MAP:
                soup_type = TYPE_MAP[a]
            elif a in LENGTH_MAP:
                length_type = LENGTH_MAP[a]

        res = await self._call_soup_api(soup_type, length_type)
        if res is None:
            puzzle, answer = self._fallback_story()
            yield event.plain_result("⚠️ 外部题库暂时不可用，使用本地测试题目。")
        else:
            puzzle, answer = res

        if not self.game_state.start_game(group_id, puzzle, answer):
            yield event.plain_result("游戏启动失败，请稍后重试。")
            return

        yield event.plain_result(
            f"🎮 海龟汤游戏开始！\n"
            f"📖 汤面：{puzzle}\n"
            f"💡 使用 /提示、/验证、/揭晓、/查看 进行游戏。\n"
            f"💡 使用 @我 或 /提问+问题 进行提问（必须加号连接，无空格）。"
        )

        await self._run_session(event, group_id, answer)

    # ---------- 提问命令 ----------
    @filter.command("提问")
    async def cmd_ask(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("请在群聊中使用。")
            return
        
        if not self.game_state.is_game_active(group_id):
            yield event.plain_result("当前没有正在进行的海龟汤游戏。请先使用 /汤 开始游戏。")
            return
        
        msg = event.message_str.strip()
        if msg.startswith("/"):
            msg = msg[1:]
        match = re.match(r'^提问\+(.+)$', msg)
        if not match:
            yield event.plain_result("格式错误！请使用：/提问+你的问题（注意加号紧挨着，无空格）")
            return
        
        question = match.group(1).strip()
        if not question:
            yield event.plain_result("请提供你要提问的内容，例如：/提问+凶手是谁？")
            return
        
        game = self.game_state.get_game(group_id)
        reply = await self._judge_question(question, game["answer"])
        game.setdefault("qa_history", []).append({"question": question, "answer": reply})
        yield event.plain_result(reply)

    # ---------- 会话控制 ----------
    async def _run_session(self, event: AstrMessageEvent, group_id: str, answer: str):
        try:
            @session_waiter(timeout=self.game_timeout, record_history_chains=False)
            async def waiter(controller: SessionController, ev: AstrMessageEvent):
                game = self.game_state.get_game(group_id)
                if not game:
                    return

                user_input = ev.message_str.strip()
                logger.debug(f"[会话] 群{group_id} 收到: {user_input}")

                if user_input in ("/汤状态", "汤状态"):
                    await self._session_status(ev, group_id)
                    controller.keep(timeout=self.game_timeout, reset_timeout=True)
                    return
                if user_input in ("/查看", "查看"):
                    await self._session_history(ev, group_id)
                    controller.keep(timeout=self.game_timeout, reset_timeout=True)
                    return
                if user_input in ("/提示", "提示"):
                    await self._session_hint(ev, group_id)
                    controller.keep(timeout=self.game_timeout, reset_timeout=True)
                    return
                if user_input.startswith("/验证") or user_input.startswith("验证"):
                    m = re.match(r"^(?:/验证|验证)\s*(.+)$", user_input)
                    if m:
                        await self._session_verify(ev, group_id, m.group(1).strip())
                        if not self.game_state.is_game_active(group_id):
                            controller.stop()
                            return
                    else:
                        await ev.send(ev.plain_result("请输入要验证的内容，例如：/验证 他是凶手"))
                    controller.keep(timeout=self.game_timeout, reset_timeout=True)
                    return
                if user_input in ("/揭晓", "揭晓"):
                    await self._session_reveal(ev, group_id)
                    controller.stop()
                    return
                
                if user_input.startswith("/提问+") or user_input.startswith("提问+"):
                    content = user_input
                    if content.startswith("/"):
                        content = content[1:]
                    m = re.match(r'^提问\+(.+)$', content)
                    if m:
                        question = m.group(1).strip()
                        if question:
                            reply = await self._judge_question(question, game["answer"])
                            game.setdefault("qa_history", []).append({"question": question, "answer": reply})
                            await ev.send(ev.plain_result(reply))
                        else:
                            await ev.send(ev.plain_result("请提供你要提问的内容，例如：/提问+凶手是谁？"))
                    else:
                        await ev.send(ev.plain_result("格式错误！请使用：/提问+你的问题（注意加号紧挨着，无空格）"))
                    controller.keep(timeout=self.game_timeout, reset_timeout=True)
                    return

                if user_input.startswith("/"):
                    return

                if not self._is_at_bot(ev):
                    return

                reply = await self._judge_question(user_input, game["answer"])
                game.setdefault("qa_history", []).append({"question": user_input, "answer": reply})
                await ev.send(ev.plain_result(reply))

                controller.keep(timeout=self.game_timeout, reset_timeout=True)

            await waiter(event, session_filter=GroupSessionFilter(group_id))

        except TimeoutError:
            game = self.game_state.get_game(group_id)
            if game:
                await event.send(event.plain_result(f"⏰ 超时！答案：{game['answer']}"))
                self.game_state.end_game(group_id)
        except Exception as e:
            logger.error(f"会话异常: {e}")
            await event.send(event.plain_result("游戏出现错误，已结束。"))
            self.game_state.end_game(group_id)

    async def _session_status(self, event, group_id):
        game = self.game_state.get_game(group_id)
        if not game:
            await event.send(event.plain_result("没有活跃游戏。"))
            return
        qa_len = len(game.get("qa_history", []))
        hint_len = len(game.get("hint_history", []))
        await event.send(
            event.plain_result(
                f"🎮 状态\n📖 汤面：{game['puzzle']}\n❓ 已提问：{qa_len} 次\n💡 已提示：{hint_len} 次"
            )
        )

    async def _session_history(self, event, group_id):
        game = self.game_state.get_game(group_id)
        hist = game.get("qa_history", []) if game else []
        if not hist:
            await event.send(event.plain_result("暂无提问记录。"))
            return
        lines = ["📋 提问记录："]
        for i, item in enumerate(hist, 1):
            lines.append(f"{i}. 问：{item['question']}\n   答：{item['answer']}")
        await event.send(event.plain_result("\n".join(lines)))

    async def _session_hint(self, event, group_id):
        game = self.game_state.get_game(group_id)
        if not game:
            await event.send(event.plain_result("没有活跃游戏。"))
            return
        hint = await self._generate_hint(game["puzzle"], game["answer"], game.get("qa_history", []))
        game.setdefault("hint_history", []).append(hint)
        await event.send(event.plain_result(f"💡 提示：{hint}"))

    async def _session_verify(self, event, group_id, guess):
        game = self.game_state.get_game(group_id)
        if not game:
            await event.send(event.plain_result("没有活跃游戏。"))
            return
        result = await self._verify_user_guess(guess, game["answer"])
        await event.send(event.plain_result(f"验证结果：{result}"))
        if "完全还原" in result or "核心推理正确" in result:
            await event.send(event.plain_result(f"🎉 恭喜！答案：{game['answer']}"))
            self.game_state.end_game(group_id)

    async def _session_reveal(self, event, group_id):
        game = self.game_state.get_game(group_id)
        if game:
            await event.send(
                event.plain_result(
                    f"🎯 游戏结束\n📖 汤面：{game['puzzle']}\n📖 汤底：{game['answer']}"
                )
            )
            self.game_state.end_game(group_id)
        else:
            await event.send(event.plain_result("没有正在进行的游戏。"))

    # ---------- 独立命令 ----------
    @filter.command("汤状态")
    async def cmd_status(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("请在群聊中使用。")
            return
        if not self.game_state.is_game_active(group_id):
            yield event.plain_result("当前没有海龟汤游戏。发送 /汤 开始吧！")
            return
        game = self.game_state.get_game(group_id)
        qa_len = len(game.get("qa_history", []))
        yield event.plain_result(f"🎮 状态\n📖 汤面：{game['puzzle']}\n已提问：{qa_len} 次")

    @filter.command("查看")
    async def cmd_history(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("请在群聊中使用。")
            return
        if not self.game_state.is_game_active(group_id):
            yield event.plain_result("请先开始游戏（/汤）")
            return
        game = self.game_state.get_game(group_id)
        hist = game.get("qa_history", [])
        if not hist:
            yield event.plain_result("暂无提问记录。")
            return
        lines = ["📋 提问记录："]
        for i, item in enumerate(hist, 1):
            lines.append(f"{i}. 问：{item['question']}\n   答：{item['answer']}")
        yield event.plain_result("\n".join(lines))

    @filter.command("提示")
    async def cmd_hint(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("请在群聊中使用。")
            return
        if not self.game_state.is_game_active(group_id):
            yield event.plain_result("请先开始游戏。")
            return
        game = self.game_state.get_game(group_id)
        hint = await self._generate_hint(game["puzzle"], game["answer"], game.get("qa_history", []))
        game.setdefault("hint_history", []).append(hint)
        yield event.plain_result(f"💡 提示：{hint}")

    @filter.command("验证")
    async def cmd_verify(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("请在群聊中使用。")
            return
        if not self.game_state.is_game_active(group_id):
            yield event.plain_result("请先开始游戏。")
            return
        
        user_input = event.message_str.strip()
        match = re.match(r"^(?:/验证|验证)\s+(.+)$", user_input)
        if not match:
            yield event.plain_result("请提供要验证的内容，例如：/验证 他是凶手")
            return
        
        guess = match.group(1).strip()
        game = self.game_state.get_game(group_id)
        result = await self._verify_user_guess(guess, game["answer"])
        yield event.plain_result(f"验证结果：{result}")
        if "完全还原" in result or "核心推理正确" in result:
            yield event.plain_result(f"🎉 恭喜！答案：{game['answer']}")
            self.game_state.end_game(group_id)

    @filter.command("揭晓")
    async def cmd_reveal(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("请在群聊中使用。")
            return
        if not self.game_state.is_game_active(group_id):
            yield event.plain_result("请先开始游戏。")
            return
        game = self.game_state.get_game(group_id)
        yield event.plain_result(f"🎯 游戏结束\n📖 汤面：{game['puzzle']}\n📖 汤底：{game['answer']}")
        self.game_state.end_game(group_id)

    # ---------- LLM 功能 ----------
    async def _judge_question(self, question: str, answer: str) -> str:
        provider = None
        if self.judge_llm_provider:
            provider = self.context.get_provider_by_id(self.judge_llm_provider)
        if provider is None:
            provider = self.context.get_using_provider()
        if provider is None:
            if question.strip() in answer:
                return "是"
            return "否"

        prompt = f"海龟汤真相：{answer}\n玩家说：{question}\n请只回答：是、否、不重要、是也不是。"
        try:
            resp: LLMResponse = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                system_prompt="你是海龟汤裁判，只回答 是/否/不重要/是也不是",
            )
            valid = {"是", "否", "不重要", "是也不是"}
            text = resp.completion_text.strip()
            return text if text in valid else "是也不是"
        except Exception as e:
            logger.error(f"判断失败: {e}")
            return "（判断出错）"

    async def _verify_user_guess(self, guess: str, answer: str) -> str:
        provider = None
        if self.judge_llm_provider:
            provider = self.context.get_provider_by_id(self.judge_llm_provider)
        if provider is None:
            provider = self.context.get_using_provider()
        if provider is None:
            if guess.strip() == answer.strip():
                return "完全还原"
            return "基本不符"

        prompt = f"标准答案：{answer}\n玩家推理：{guess}\n请判断准确度（完全还原/核心推理正确/部分正确/基本不符）。"
        try:
            resp: LLMResponse = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                system_prompt="你是海龟汤裁判，评价推理准确性。",
            )
            return resp.completion_text.strip()
        except Exception as e:
            logger.error(f"验证失败: {e}")
            return "验证出错"

    async def _generate_hint(self, puzzle: str, answer: str, qa_history: list) -> str:
        provider = None
        if self.judge_llm_provider:
            provider = self.context.get_provider_by_id(self.judge_llm_provider)
        if provider is None:
            provider = self.context.get_using_provider()
        if provider is None:
            return "尝试思考动机方面的线索。"

        hist = "\n".join([f"问：{q['question']} 答：{q['answer']}" for q in qa_history]) if qa_history else "暂无"
        prompt = f"题面：{puzzle}\n真相（勿泄）：{answer}\n历史问答：{hist}\n给出一个不泄密的启发性提问方向。只输出最简短的话"
        try:
            resp: LLMResponse = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                system_prompt="你是提示助手。",
            )
            return resp.completion_text.strip()
        except Exception as e:
            logger.error(f"生成提示失败: {e}")
            return "尝试思考动机方面的线索。"

    def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        bot_id = str(event.get_self_id())
        for comp in event.message_obj.message:
            if isinstance(comp, At) and str(comp.qq) == bot_id:
                return True
        return False