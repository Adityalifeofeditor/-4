import os
import requests
import time


url = os.environ.get("url", "https://repo_name.onrender.com")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

#----------------------------------------------------------------------------------------------

if url:
    web_link = "https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={url}"
    try:
        response = requests.get(web_link)
        print("Status Code:", response.status_code)
    except Exception as err:
        print(f"An error occurred: {str(err)}")
    #except requests.exceptions.RequestException as e:
        #print("An error occurred:", e)

#----------------------------------------------------------------------------------------------    

while True:
    try:
        response = requests.get(url)
        print("Status Code:", response.status_code)
    #except requests.exceptions.RequestException as e:
        #print("An error occurred:", e)
    except Exception as err:
        print(f"An error occurred: {str(err)}")
    time.sleep(30)  # Wait for 15 seconds before the next request
