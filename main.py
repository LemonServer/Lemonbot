import traceback
from time import sleep
from UIfunction import WxRead
from UIfunction import WxSend
from UIfunction import judge
import zhipu_api
from journal_append import journal_append
from data import Wx, Message
def main():
# 初始化
    print("Lemon bot为您服务，我是基于智谱ai的ChatGLM4机器人\n正在初始化，请稍候！\n")
    diagAll = Wx.ListControl(Name="会话")
    i = 0
# 启动成功
    Wx.ButtonControl(Name="易安").Click()
    WxSend("易安", "初始化成功")
    print("初始化成功")
    journal_append("bot日志.txt", "初始化成功")
# 程序开始运行
    try:
        while (True):
            if len(diagAll.GetChildren()[i].GetFirstChildControl().GetChildren()) == 3:
                print(diagAll.GetChildren()[i].Name + "有新消息")#判断是否有新消息
                diagAll.GetChildren()[i].Click()#选中新消息的对话框
                if (WxRead()):
                    question = Message[-1][1]#从已读取消息的列表中找出最后一条
                    name = Message[-1][0]#找出提问者
                    question = question[11:] if judge() == '群聊' else question#针对群聊，去除“@”字符串
                    print("正在处理")
                    answer = zhipu_api.glm(question)#调用api，给出ai的回答
                    print("Lemon bot:", answer)
                    WxSend(name, answer)#发送给提问者
                    journal_append("bot日志.txt", "   Lemon answered a question and sent a message.\n")
            #刷新列表的序号
            if i == 0:
                diagAll = Wx.ListControl(Name="会话")
            i = (i + 1) % 5
    except Exception:
    # 捕获异常，并将异常信息写入到文件，通知开发者
        journal_append("bot日志.txt", traceback.format_exc())
        Wx.ButtonControl(Name="易安").Click()
        WxSend("易安", traceback.format_exc())
    return 1#出现问题，即将尝试自动重启
