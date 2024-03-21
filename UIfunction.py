import pyautogui
import pyperclip
from time import sleep
from data import Wx,Message,password
def judge():
    if Wx.ToolBarControl(Name='',LocalizedControlType='工具栏').GetLastChildControl().Name == "视频聊天":
        return("私聊")
    else:
        return("群聊")

def WxSend(name, answer):
    # 用来发送消息
    Wx.SwitchToThisWindow()
    if judge()=='群聊':
        Wx.SendKeys('@', waitTime=0)
        Wx.SendKeys(name, waitTime=0)
        Wx.SendKeys('   ', waitTime=0)
    pyperclip.copy(answer)#将回答写入剪贴板
    Wx.ButtonControl(Name="发送(S)").Click()#点击发送按钮确保激活输入光标
    sleep(2)
    pyautogui.hotkey('ctrl', 'v')#发送
    pyautogui.hotkey('enter')
def WxRead():
    global Wx, Message, password
    # 用来获取消息
    Wx.SwitchToThisWindow()
    mail_group = Wx.ListControl(Name="消息").GetChildren()
    mail = mail_group[-1]
    if (mail.GetFirstChildControl().Name == ''):  # 这一条是判断是否是信息发布时间
        if (mail.GetFirstChildControl().GetFirstChildControl().Name != ''):  # 这一条是判断信息是否是自己发出的
            if judge() == '群聊':
                if password in mail.Name:
                    Message.append([mail.GetFirstChildControl().GetFirstChildControl().Name, mail.Name])
                    return 1
            else:
                Message.append([mail.GetFirstChildControl().GetFirstChildControl().Name, mail.Name])
                return 1
