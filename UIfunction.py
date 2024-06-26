import pyautogui
import pyperclip
from time import sleep
from context import UserMessageContext
from data import Wx,Message,password,number
def judge():
    global Wx
    if Wx.ToolBarControl(Name='',LocalizedControlType='工具栏').GetLastChildControl().Name == "视频聊天":
        return("私聊")
    else:
        return("群聊")

def Message_append(username,question):
    global Message
    for i in range(len(Message)):
        if Message[i].username == username:
            Message[i].add_user_question(question)
            return i
    i = len(Message)
    Message.append(UserMessageContext(username))
    Message[i].add_user_question(question)
    return i

def WxSend(name, answer):
    # 用来发送消息
    global Wx
    Wx.SwitchToThisWindow()
    if judge()=='群聊':
        Wx.SendKeys('@', waitTime=0)
        Wx.SendKeys(name, waitTime=0)
        Wx.SendKeys('   ', waitTime=0)
    pyperclip.copy(answer)      # 将回答写入剪贴板
    Wx.ButtonControl(Name="发送(S)").Click()    # 点击发送按钮确保激活输入光标
    sleep(2)
    pyautogui.hotkey('ctrl', 'v')   # 发送
    pyautogui.hotkey('enter')
def WxRead():
    global Wx, Message, password,number
    # 用来获取消息
    Wx.SwitchToThisWindow()
    mail_group = Wx.ListControl(Name="消息").GetChildren()
    mail = mail_group[-1]
    if (mail.GetFirstChildControl().Name == ''):    # 这一条是判断是否是信息发布时间
        if (mail.GetFirstChildControl().GetFirstChildControl().Name != ''):     # 这一条是判断信息是否是自己发出的
            question = mail.Name
            if judge()=="群聊":
                if password in question:
                    question=question[11:]
                    number[0]=Message_append(mail.GetFirstChildControl().GetFirstChildControl().Name, question)
                    print(number[0])
                    return 1
                else:
                    return 0
            else:
                number[0]=Message_append(mail.GetFirstChildControl().GetFirstChildControl().Name, question)
                print(number[0])
                return 1
