import uiautomation
from zhipuai import ZhipuAI
import context
#本工程中将要使用的重要全局变量
Wx = uiautomation.WindowControl(Name="微信")    # 微信窗口
Message = []    # 用来储存用户对话信息，MessageList 类对象
password = '@Lemon bot'     # 群聊中的"@"
api="请输入你的 api"
client=ZhipuAI(api_key=api)
number=-1
