from app.wechat import WeChatBot
from app.chatbot import Ai
from app.storage import Storage
if __name__ == '__main__':
       storage = Storage('data/conversations.db')
       chatbot = Ai(api_key='your_openai_api_key')
       wechat_bot = WeChatBot(chatbot, storage)
       wechat_bot.run()

