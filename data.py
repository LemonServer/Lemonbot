import uiautomation
from zhipuai import ZhipuAI
import context
#本工程中将要使用的重要全局变量
Wx = uiautomation.WindowControl(Name="微信")#微信窗口
Message = [context.UserMessageContext(i) for i in range(10)]  # 用来储存待处理信息的变量
password = '@Lemon bot'#群聊中的"@"
api="d4d3b6869f22136f121d9b6dfc475bc0.ZD9dl84s6ZydwcMj"
client=ZhipuAI(api_key=api)
number=-1
