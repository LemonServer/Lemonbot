from datetime import datetime


def journal_append(journal_name, content):
    file = open(journal_name, mode='a')
    file.write("\n")
    current_time = datetime.now()
    file.write(str(current_time))
    file.write(content)
    file.close()
