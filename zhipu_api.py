from zhipuai import ZhipuAI
from data import client
global client
def glm(user_message):
    response = client.chat.completions.create(
        model="glm-4",  # 填写需要调用的模型名称
        messages=[
            {"role": "user", "content": "你是Lemon bot，基于智谱ai的ChatGLM4微信聊天机器人"},
            {"role": "assistant","content": "是的，我是基于智谱AI的ChatGLM4微信聊天机器人，你可以叫我Lemon bot。我旨在通过微信平台，为用户提供自然、流畅的对话体验，协助回答问题、提供信息和建议。利用ChatGLM4的技术，我可以理解和生成自然语言，帮助用户解决各种问题。如果你有任何疑问或需要帮助，请随时告诉我。"},
            {"role": "user", "content": user_message}#输入prompt以及用户提出的问题
        ]
    )
    ai_message = response.choices[0].message.content#获取返回的ai输出
    return ai_message
