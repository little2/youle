import asyncio
import random
import os
import json
import aiomysql
from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import ChatPermissions
from aiogram.client.default import DefaultBotProperties
from game_panty_template import PANTY_MOVE_TEMPLATES, SCENE_TEMPLATES,IMAGE_REWARD_MAP
from aiogram.exceptions import TelegramBadRequest

from aiogram import BaseMiddleware
from aiogram.types import Update

import asyncio
import time

# 加载环境变量
if not os.getenv('GITHUB_ACTIONS'):
    from dotenv import load_dotenv
    load_dotenv('.game.env')




API_TOKEN = os.getenv('API_TOKEN')
MYSQL_DB_NAME = os.getenv('MYSQL_DB_NAME')
MYSQL_DB_USER = os.getenv('MYSQL_DB_USER')
MYSQL_DB_PASSWORD = os.getenv('MYSQL_DB_PASSWORD')
MYSQL_DB_HOST = os.getenv('MYSQL_DB_HOST')
MYSQL_DB_PORT = int(os.getenv('MYSQL_DB_PORT', 3306))

bot = Bot(API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()

games = {}  # 群组游戏实例

# 防止群内重复 restart
is_restarting = {}
game_message_id = 0  # 全局变量，记录当前游戏消息 ID

NAME_POOL = ["依依", "小姚", "小胖", "小唯", "球球", "小宇", "童童", "俊伟", "小石头", "飞飞"]
POINT_COST = 15
POINT_REWARD = 30
DEFAULT_POINT = 0

# ===== 新增：统一运营时限 =====
MAX_RUNTIME_SEC = 15 * 60          # 15 分钟
START_TS = time.time()             # 程序启动时间
SHUTDOWN_REQUESTED = False         # 标记：是否已到达关机时限



class ThreadSafeThrottleMiddleware(BaseMiddleware):
    def __init__(self, rate_limit: float = 1.0):
        super().__init__()
        self.rate_limit = rate_limit
        self._user_time = {}
        self._lock = asyncio.Lock()

    async def __call__(self, handler, event: Update, data: dict):
        user_id = event.from_user.id if event.from_user else None
        now = time.monotonic()

        if user_id:
            async with self._lock:
                last_time = self._user_time.get(user_id, 0)
                if now - last_time < self.rate_limit:
                    return  # ✅ 直接结束，不继续传递
                self._user_time[user_id] = now

        return await handler(event, data)




# ========== MySQL Manager ==========
class MySQLPointManager:
    def __init__(self, pool):
        self.pool = pool



    async def get_user_point(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT point FROM user WHERE user_id = %s", (user_id,))
                row = await cur.fetchone()
                return row[0] if row else 0

    async def update_user_point(self, user_id: int, delta: int):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("UPDATE user SET point = point + %s WHERE user_id = %s", (delta, user_id))
                await conn.commit()

    async def get_or_create_user(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT point FROM user WHERE user_id = %s", (user_id,))
                row = await cur.fetchone()
                if row:
                    return row[0]
                else:
                    await cur.execute("INSERT INTO user (user_id, point) VALUES (%s, %s)", (user_id, DEFAULT_POINT))
                    await conn.commit()
                    return DEFAULT_POINT


# ========== 游戏类 ==========
class PantyRaidGame:



    def __init__(self, image_file_id, chat_id: int, thread_id: int, message_id: int):
        self.image_file_id    = image_file_id
        self.reward_file_id   = IMAGE_REWARD_MAP.get(image_file_id)
        self.chat_id          = chat_id
        self.thread_id        = thread_id
        self.message_id       = message_id    # ← 保存这局的消息 ID
        self.names            = random.sample(NAME_POOL, 4)
        self.true_boy         = random.choice(self.names)
        self.claimed          = {}
        self.finished         = False
        self.lock             = asyncio.Lock()
        asyncio.create_task(self.auto_timeout_checker())

    def is_all_claimed(self):
        return len(self.claimed) == 4

    def markup_to_json(self, markup):
      
        return json.dumps(markup.model_dump(), sort_keys=True) if markup else ''

    def get_game_description(self):
        return (
            "🎭 <b>脱裤大作战开始！</b>\n\n"
            "四个弟弟排排站，只有一个是小基弟弟！\n\n"
            "快站到你怀疑的弟弟面前，大会会开始播放AV，等一声令下——脱！裤！\n"
            "真相只有一个，看你能不能一眼识破！\n\n"
            f"每次脱裤需要消耗 {POINT_COST} 积分。\n"
            "四个弟弟中，只有一位是看了AV不是 JJ In In De。\n"
            f"猜中可获得 {POINT_REWARD} 积分奖励以及脱裤后的照片！😊\n\n"
            "🩲 请选择你要锁定的目标：(可多选)"
        )

    def get_keyboard(self):
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=f"🩲 {name}", callback_data=f"panty_{name}")]
                             for name in self.names]
        )

    # async def auto_timeout_checker(self):
    #     await asyncio.sleep(60)  # 等待 60 秒
    #     async with self.lock:
    #         if not self.finished:
    #             self.finished = True
    #             print("⌛ 游戏超时，自动揭晓结果")
    #             try:
    #                 # 删除原下注消息（防止点击）
    #                 if self.claimed:
    #                     # 找一个玩家的 message 去揭晓（偷懒做法）
    #                     any_uid = next(iter(self.claimed.values()))['user_id']
    #                     any_chat_id = None
    #                     for g_chat_id, g in games.items():
    #                         if g is self:
    #                             any_chat_id = g_chat_id
    #                             break
    #                     if any_chat_id:
    #                         await self.reveal_results_by_chat_id(any_chat_id)
    #                 else:
    #                     # 删除信息 
    #                     await self.reveal_results_by_chat_id(self.chat_id)
    #             except Exception as e:

    #                 print(f"⚠️ 自动揭晓失败：{e}")

    async def auto_timeout_checker(self):
        await asyncio.sleep(60)
        async with self.lock:
            if self.finished:
                return
            self.finished = True
        # 删除原游戏消息，防止再点击
        try:
            await bot.delete_message(chat_id=self.chat_id, message_id=self.message_id)
        except TelegramBadRequest:
            pass
        await self.reveal_timeout()

    async def reveal_timeout(self):
        # 统一揭晓流程
        lines = [f"⌛ 超时自动揭晓！真·小基弟弟是：<span class='tg-spoiler'>{self.true_boy}</span>\n"]
        winner = None

        # 先看谁猜中了
        for name, who in self.claimed.items():
            if name == self.true_boy:
                winner = who
                lines.append(f"🎉 恭喜 <u>{who['user_name']}</u> 猜对了，获得 {POINT_REWARD} 积分！")
                await point_manager.update_user_point(who['user_id'], POINT_REWARD)

        # 如果没人猜中，但有人参与，则随机挑一位发安慰奖
        if not winner and self.claimed:
            losers = list(self.claimed.values())
            who = random.choice(losers)
            winner = who
            half = POINT_REWARD // 2
            lines.append(f"🔔 没有人猜中，随机安慰奖励给 <u>{who['user_name']}</u>，获得 {half} 积分！")
            await point_manager.update_user_point(who['user_id'], half)

        # 如果根本没人参与
        if not self.claimed:
            await bot.send_message(
                chat_id=self.chat_id,
                message_thread_id=self.thread_id,
                text="⌛️ 本轮无人下注，欢迎再来一局 🩲",
                parse_mode=ParseMode.HTML,
                reply_markup=get_restart_keyboard()    # ← 加上再来一局按钮
            )
            return

        # 发送揭晓，并附上「再来一局」或「领奖」按钮
        markup = get_winner_keyboard(winner['user_id']) if winner and winner['user_id'] in {c['user_id'] for c in self.claimed.values()} else get_restart_keyboard()
        await bot.send_message(
            chat_id=self.chat_id,
            message_thread_id=self.thread_id,
            text="\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=markup
        )
        
    async def reveal_results_by_chat_id(self, chat_id: int):
        try:
            # ✅ 先移除旧图的按钮（如果还没删）
            try:
                await bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=game_message_id,
                    reply_markup=None
                )
            except TelegramBadRequest as e:
                if "message is not modified" in str(e):
                    print("⚠️ 按钮已经为空，无需修改")
                else:
                    print(f"⚠️ 无法清除旧按钮: {e}")
          

            # ✅ 删除整条旧游戏消息
            try:
                await bot.delete_message(chat_id=chat_id, message_id=game_message_id)
            except TelegramBadRequest as e:
                print(f"⚠️ 删除旧消息失败: {e}")

            # ✅ 然后发送自动揭晓
            result_msg = await bot.send_message(
                chat_id=chat_id,
                message_thread_id=self.message_thread_id,
                text=(
                    # f"⏱️ 一分钟过去了，自动揭晓：\n\n"
                    # f"🔔 小基弟弟是：<span class='tg-spoiler'>{self.true_boy}</span>\n\n"
                    f"本轮无人猜中，欢迎再来一局 🩲"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=get_restart_keyboard()
            )
        except Exception as e:
            print(f"⚠️ reveal_results_by_chat_id 出错：{e}")




    async def handle_panty(self, callback: CallbackQuery, choice: str):
        async with self.lock:
            user_id = callback.from_user.id
            user_name = callback.from_user.full_name

            if self.finished:
                await safe_callback_answer(callback, "游戏已结束。", True)
                return

            if choice in self.claimed:
               
                await safe_callback_answer(callback, f"这个弟弟已被 {self.claimed[choice]['user_name']} 预订！", True)
                return

            points = await point_manager.get_or_create_user(user_id)
            if points < POINT_COST:
                await safe_callback_answer(callback, "你的积分不够啦！", True)
                return

            await point_manager.update_user_point(user_id, -POINT_COST)
            await safe_callback_answer(callback, f"你下注了 {POINT_COST} 积分，目前剩下 {(points-POINT_COST)} 分！", True)
            self.claimed[choice] = {'user_id': user_id, 'user_name': user_name}
            

            # 优先改按钮，防止重复点击
            new_markup = self.disable_button(callback.message.reply_markup, choice)
            old_markup_json = self.markup_to_json(callback.message.reply_markup)
            new_markup_json = self.markup_to_json(new_markup)

            if old_markup_json != new_markup_json:
                try:
                    await callback.message.edit_reply_markup(reply_markup=new_markup)
                    
                except TelegramBadRequest as e:
                    if "message is not modified" in str(e):
                        print("⚠️ 忽略 message is not modified 错误")
                    else:
                        raise e
            else:
                print("⏭️ reply_markup 未变化，跳过 edit")



            await callback.message.answer(random.choice(PANTY_MOVE_TEMPLATES).format(user_name="<u>"+user_name+"</u>",choice= "<u>"+choice+"</u>"))
            

            if self.is_all_claimed():
                try:
                    await callback.message.delete()
                except Exception as e:
                    print(f"删除下注消息失败: {e}")  # 可以忽略
                await self.reveal_results(callback)
                
                

    async def reveal_results(self, callback: CallbackQuery):
        self.finished = True
        winner_uid = None
        summary_lines = [f"🔔 小基弟弟是：<span class='tg-spoiler'>{self.true_boy}</span>\r\n"]

        for name, claimer in self.claimed.items():
            uid = claimer['user_id']
            uname = claimer['user_name']
            if name == self.true_boy:
                winner_uid = uid
                text = random.choice(SCENE_TEMPLATES).format(player=uname, target=name, result=f"小基弟弟，🎉 获得 {POINT_REWARD} 积分！")
                summary_lines.append(f"<span class='tg-spoiler'>{text}</span>")
                await point_manager.update_user_point(uid, POINT_REWARD)


        bot_username = (await bot.get_me()).username
        notice = f"\r\n⚠️ 请赢家先私聊 <a href='https://t.me/{bot_username}'>@{bot_username}</a> 领取奖励！"
        summary_lines.append(notice)

        reply_markup = get_winner_keyboard(winner_uid) if winner_uid else None
        # await callback.message.answer("\n".join(summary_lines), reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        result_msg = await callback.message.answer("\n".join(summary_lines), reply_markup=reply_markup, parse_mode=ParseMode.HTML)

        # 启动领奖超时任务
        if winner_uid:
            asyncio.create_task(self.wait_for_reward_timeout(result_msg))
    # def disable_button(self, keyboard: InlineKeyboardMarkup, choice: str) -> InlineKeyboardMarkup:
    #     return InlineKeyboardMarkup(inline_keyboard=[
    #         [InlineKeyboardButton(text=f"{btn.text}（已被选定）", callback_data="disabled") if btn.callback_data == f"panty_{choice}" else btn for btn in row]
    #         for row in keyboard.inline_keyboard
    #     ])


    async def wait_for_reward_timeout(self, result_msg: Message):
        await asyncio.sleep(25)  # 等待 15 秒
        try:
            # 取出当前按钮的 callback_data
            if result_msg.reply_markup and result_msg.reply_markup.inline_keyboard:
                current_callback_data = result_msg.reply_markup.inline_keyboard[0][0].callback_data
                if current_callback_data and current_callback_data.startswith("reward_"):
                    # 按钮还在领奖状态，替换成再来一局

                    old_markup_json = json.dumps(result_msg.reply_markup.model_dump(), sort_keys=True) if result_msg.reply_markup else ''
                    new_markup_json = json.dumps(get_restart_keyboard().model_dump(), sort_keys=True)

                    if old_markup_json != new_markup_json:
                        try:
                            await result_msg.edit_reply_markup(reply_markup=get_restart_keyboard())
                        except TelegramBadRequest as e:
                            if "message is not modified" in str(e):
                                print("⚠️ 跳过重复修改 reply_markup（wait_for_reward_timeout）")
                            else:
                                print(f"处理领奖超时失败 (TelegramBadRequest): {e}")
                    else:
                        print("⏭️ reply_markup 已是 restart_keyboard，跳过 edit")


                    
        except Exception as e:
            print(f"处理领奖超时失败: {e}")


    def disable_button(self, keyboard: InlineKeyboardMarkup, choice: str) -> InlineKeyboardMarkup:
        new_kb = []
        for row in keyboard.inline_keyboard:
            new_row = []
            for button in row:
                if button.callback_data != f"panty_{choice}":
                    new_row.append(button)  # 只保留未被选中的按钮
            if new_row:
                new_kb.append(new_row)
        return InlineKeyboardMarkup(inline_keyboard=new_kb)


# ========== 通用按钮 ==========
def get_winner_keyboard(winner_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🎁 领取脱下裤子的照片", callback_data=f"reward_{winner_id}")]]
    )

def get_restart_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔄 再来一局", callback_data="restart_game")]]
    )

# ========== 防止旧 Query 错误 ==========
async def safe_callback_answer(callback: CallbackQuery, text: str, show_alert: bool = False):
    try:
        await callback.answer(text, show_alert=show_alert)
    except Exception as e:
        print(f"忽略 query 错误: {e}")


def runtime_exceeded() -> bool:
    return (time.time() - START_TS) >= MAX_RUNTIME_SEC

# ========== 游戏控制 ==========
@router.message(Command("start_pantyraid"))
async def start_game(message: Message):
    if runtime_exceeded():                 # <-- 新增
        await message.answer("⏰ 中场休息10分钟。")
        return
    chat_id = message.chat.id
    thread_id = getattr(message, 'message_thread_id', None)  # 支援主题串

    # 防止重复开启游戏
    existing_game = games.get(chat_id)
    if existing_game and not existing_game.finished:
        await message.answer("⚠️ 本局游戏尚未结束，请先完成当前游戏再开启新局！")
        return

    # 开启新游戏
    await start_new_game(chat_id, message)

async def shutdown_after_timeout(dispatcher: Dispatcher):
    global SHUTDOWN_REQUESTED
    await asyncio.sleep(MAX_RUNTIME_SEC)
    SHUTDOWN_REQUESTED = True
    print("⏰ 达到弟弟工时限制，等待当前回合结束…")

    # ✅ 第一阶段：等待所有游戏正常结束
    while any(not g.finished for g in games.values()):
        await asyncio.sleep(2)

    print("🧺 全部游戏回合结束，准备广播结束消息")

    # ✅ 第二阶段：通知各群组
    for chat_id, game in games.items():
        try:
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=game.message_thread_id,
                text="🕓 营业时间结束，弟弟们要回更衣室休息了～\n欢迎稍后再来一局 🩲",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            print(f"⚠️ 无法在群 {chat_id} 发送结束提示：{e}")
        await asyncio.sleep(1)  # 防止被 Telegram rate limit

    print("🎮 已发送所有结束消息，准备停止 polling")
    await dispatcher.stop_polling()
    await bot.session.close()
    print("✅ 弟弟们已回休息室了")



@router.callback_query(F.data.startswith("panty_"))
async def handle_panty(callback: CallbackQuery):



    chat_id = callback.message.chat.id
    game = games.get(chat_id)
    if game:
        await game.handle_panty(callback, callback.data.split("_")[1])
    else:
        await safe_callback_answer(callback, "游戏未开始或已结束。", True)

@router.callback_query(F.data.startswith("reward_"))
async def handle_reward(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    game = games.get(chat_id)
    if not game:
        await safe_callback_answer(callback, "❌ 本轮游戏不存在或已结束。", True)
        return

    winner_id = int(callback.data.split("_")[1])
    if callback.from_user.id != winner_id:
        await safe_callback_answer(callback, "❌ 你不是赢家，不能领取奖励！", True)
        return

    if not game.reward_file_id:
        await safe_callback_answer(callback, "⚠️ 此轮没有设置奖励图片。", True)
        return


    

    
    try:
        await callback.message.edit_reply_markup(reply_markup=get_restart_keyboard())
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            print("⚠️ 跳过重复修改 reply_markup")
        else:
            raise e


   
    try:
        await bot.send_photo(callback.from_user.id, photo=game.reward_file_id, caption="🎉 这是你的奖励！")
        await safe_callback_answer(callback, "奖励已发送到你的私聊！", True)
    except Exception as e:
        print(f"⚠️ 无法私聊用户 {callback.from_user.id}，用户未启动 bot: {e}")
        

@router.callback_query(F.data == "restart_game")
async def handle_restart_game(callback: CallbackQuery):
    chat_id = callback.message.chat.id

    # ✅ 如果当前群正在 restart，直接忽略
    if is_restarting.get(chat_id, False):
        print(f"⚠️ 群 {chat_id} 正在 restart 中，忽略重复点击")
        await safe_callback_answer(callback, "⚠️ 正在开启新一局，请稍候～", True)
        return

    # ✅ 标记正在 restart
    is_restarting[chat_id] = True

    # 尝试清除按钮
    if callback.message.reply_markup is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                print("⚠️ 忽略 message is not modified 错误（handle_restart_game）")
            else:
                raise e
    else:
        print("⏭️ 当前消息本来就没有 reply_markup，跳过 edit_reply_markup")

    try:
        await start_new_game(chat_id, callback.message)
        await safe_callback_answer(callback, "已开启新一局！")
    finally:
        # ✅ 无论是否出错，最后要解锁
        is_restarting[chat_id] = False


@router.message(Command("points"))
async def check_points(message: Message):
    points = await point_manager.get_or_create_user(message.from_user.id)
    await message.answer(f"🪙 你目前有 {points} 积分")

@router.message(F.photo)
async def handle_photo(message: Message):
    # 取最后一张（通常是最高分辨率）
    photo = message.photo[-1]
    file_id = photo.file_id
    file_unique_id = photo.file_unique_id
    await message.reply(f"🖼️ 你发的图片 file_id 是：<code>{file_unique_id}</code>\r\n<code>{file_id}</code>")


@router.message(Command("start"))
async def start_command(message: Message):
    args = message.text.split(" ")
    if len(args) > 1 and args[1] == "free":
        user_id = message.from_user.id

        FREE_CHAT_ID=-1002630327230


        permissions = ChatPermissions(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True
        )

        try:
            await bot.restrict_chat_member(
                chat_id=FREE_CHAT_ID,
                user_id=user_id,
                permissions=permissions
            )
            await message.answer(f"✅ 已为你解除群 {FREE_CHAT_ID} 内的发言 & 发送媒体权限！")
        except TelegramBadRequest as e:
            await message.answer(f"⚠️ 无法解除限制: {e}")
        except Exception as e:
            await message.answer(f"⚠️ 出现错误: {e}")
    else:
        await message.answer("🤖 你好！欢迎使用机器人。")



# ========== 启动新游戏 ==========
# async def start_new_game(chat_id: int, message: Message):
#     global game_message_id  # ✅ 添加这行
#     image_file_id = random.choice(list(IMAGE_REWARD_MAP.keys()))
#     # game = PantyRaidGame(image_file_id, chat_id=chat_id, message_thread_id=message.message_thread_id )
#     # games[chat_id] = game
#     # await message.answer_photo(photo=image_file_id, caption=game.get_game_description(), reply_markup=game.get_keyboard())
   

#     game = PantyRaidGame(
#         image_file_id,
#         chat_id=chat_id,
#         message_thread_id=getattr(message, "message_thread_id", None)
#         ret.message_id
#     )
#     games[chat_id] = game

#     ret = await message.answer_photo(
#         photo=image_file_id,
#         caption=game.get_game_description(),
#         reply_markup=game.get_keyboard()
#     )
   
#     game_message_id = ret.message_id if ret else 0
#     print(f"✅ {game_message_id}")

# ========== 启动新游戏 ==========
async def start_new_game(chat_id: int, message: Message):
    # 1. 随机选一张图
    image_file_id = random.choice(list(IMAGE_REWARD_MAP.keys()))
    # 2. 暂时把 message_id 设成 None，先创建实例
    thread_id = getattr(message, "message_thread_id", None)
    game = PantyRaidGame(image_file_id, chat_id, thread_id, message_id=None)
    games[chat_id] = game

    # 3. 发消息，用 game 的描述和键盘
    ret = await message.answer_photo(
        photo=image_file_id,
        caption=game.get_game_description(),
        reply_markup=game.get_keyboard()
    )

    # 4. 收到 message_id 后再回写到实例里
    game.message_id = ret.message_id


# ========== 数据库连接 ==========
async def init_mysql_pool():
    return await aiomysql.create_pool(
        host=MYSQL_DB_HOST,
        port=MYSQL_DB_PORT,
        user=MYSQL_DB_USER,
        password=MYSQL_DB_PASSWORD,
        db=MYSQL_DB_NAME,
        autocommit=True
    )

# ========== 启动 ==========
async def main():
    global point_manager
    pool = await init_mysql_pool()
    point_manager = MySQLPointManager(pool)

    # 添加限速中间件（2秒限制一次）
    dp.message.middleware(ThreadSafeThrottleMiddleware(rate_limit=1.0))
    dp.callback_query.middleware(ThreadSafeThrottleMiddleware(rate_limit=1.0))

    dp.include_router(router)
    

    # ===== 新增：启动关机计时器 =====
    asyncio.create_task(shutdown_after_timeout(dp))

    try:
        await dp.start_polling(bot)
    finally:
        # ✅ 确保关闭连接池
        print("🔌 正在关闭 MySQL 连接池…")
        pool.close()
        await pool.wait_closed()
        print("✅ MySQL 连接池已关闭")

    # 遍历IMAGE_REWARD_MAP,并发发送图片

    # for key, value in IMAGE_REWARD_MAP.items():
    #     await bot.send_photo(8150238704, photo=key)
    #     await bot.send_photo(8150238704, photo=value)



if __name__ == "__main__":
    asyncio.run(main())
