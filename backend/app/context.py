from datetime import datetime


class UserMessageContext():

    def __init__(self, username):  # 初始化对象，设置用户名，记录初始化时间点
        self.username = username
        self.register_time = datetime.now()

        self.MessageList = [
            {"role": "user", "content": "你是Lemon bot，基于智谱ai的ChatGLM4微信聊天机器人"},
            {"role": "assistant",
             "content": "是的，我是基于智谱AI的ChatGLM4微信聊天机器人，你可以叫我Lemon "
                        "bot。我旨在通过微信平台，为用户提供自然、流畅的对话体验，协助回答问题、提供信息和建议。利用ChatGLM4"
                        "的技术，我可以理解和生成自然语言，帮助用户解决各种问题。如果你有任何疑问或需要帮助，请随时告诉我。"}
        ]  # 对话内容

    def add_user_question(self, question):  # 添加用户问题
        user_question = {"role": "user", "content": question}
        self.MessageList.append(user_question)

    def add_ai_answer(self, answer):  # 添加 AI 回复
        ai_answer = {"role": "assistant", "content": answer}
        self.MessageList.append(ai_answer)

    def __del__(self):
        print("本次与 " + self.username + " 的上下文对话已完成")
