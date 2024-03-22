from zhipuai import ZhipuAI
from data import client
global client
def glm(message_list):
    response = client.chat.completions.create(
        model="glm-4",  # 填写需要调用的模型名称
        messages=message_list
    )
    return response.choices[0].message.content#获取返回的ai输出
