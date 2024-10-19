#Componentes clave: Se utiliza FastAPI para crear un servidor web, 
# gspread para interactuar con Google Sheets, 
# requests para hacer solicitudes a la API de Siigo y smtplib para enviar correos electrónicos.

#Webhook: La forma más común de recibir datos de un formulario es a través de un webhook
#bibliotecas estandar de python
import json
import smtplib
from typing import Optional

#bibliotecas de terceros necesarias para el funcionamiento del codigo
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import uvicorn
import os
import time
import uuid
import httpx
from dotenv import load_dotenv


#bibliotecas propias del proyecto
from siigo_api import SiigoAPIError
from email_error import EmailError
from whatsapp import send_whatsapp_message



app = FastAPI()
load_dotenv()   #para manejar las variables de entorno

#Parametros de configuracion de la API para Siigo (Restricciones de uso), accesos a la API, 
SIIGO_API_URL = "https://api.siigo.com"
SIIGO_AUTH_URL = f"{SIIGO_API_URL}/auth"
SIIGO_PARTNER_ID = "AQUI IRIA EL PARTNER ID REAL"
SIIGO_API_USERNAME = "AQUI IRIA EL USERNAME REAL"
MAX_REQUESTS_PER_SECOND = 3
RETRY_DELAY = 1
SMTP_SERVER = "smtp-relay.gmail.com"
SMTP_PORT = 587
SMTP_USERNAME = "transformaciondigital@timbale.com"
SMTP_PASSWORD = "TDigital24!!"
PORT=8000

# Pydantic model for user registration data
class UserRegistration(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone: str
    identification: str
    address: Optional[str] = ""
    city: Optional[str] = ""

# Google Sheets setup
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_name('C:\\Users\\alvar\\OneDrive\\Documentos\\TIMBALE\\V0\\client_secret_350408092788-vbga9f6ng1fkc2pmnk9uc1sgsp480e8b.apps.googleusercontent.com(1).json', SCOPES)
client = gspread.authorize(creds)
sheet = client.open("Base_datos_Prueba_Integracion").formato_oficial

# Siigo API setup
SIIGO_API_URL = "https://api.siigo.com"
SIIGO_AUTH_URL = f"{SIIGO_API_URL}/auth"

# Gmail API setup
GMAIL_CREDS = Credentials.from_authorized_user_file('C:\\Users\\alvar\\OneDrive\\Documentos\\TIMBALE\\V0\\client_secret_350408092788-vbga9f6ng1fkc2pmnk9uc1sgsp480e8b.apps.googleusercontent.com(1).json', ['https://www.googleapis.com/auth/gmail.send'])
gmail_service = build('gmail', 'v1', credentials=GMAIL_CREDS)

#manejo de errores de la API
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "code": exc.status_code}
        )

#funcion para obtener el token de acceso de Siigo basado en el token brindado por la gente de soporte de siigo
def get_siigo_token():  
    headers = {
        "Authorization": f"Basic {os.getenv('SIIGO_API_TOKEN')}",
        "Content-Type": "application/json",
        "Partner-Id": os.getenv('SIIGO_PARTNER_ID')
    }
    auth_data={
        "username": os.getenv('SIIGO_API_USERNAME')  #usuario de prueba para la api de siigo 
    }
    for attempt in range(MAX_REQUESTS_PER_SECOND):
        try:
            response = requests.post(SIIGO_AUTH_URL, json=auth_data, headers=headers)
            response.raise_for_status()
            return response.json()["NDllMzI0NmEtNjExZC00NGM3LWE3OTQtMWUyNTNlZWU0ZTM0OkosU2MwLD4xQ08="]
        except requests.exceptions.RequestException as e:
            if attempt == MAX_REQUESTS_PER_SECOND - 1:
                raise HTTPException(status_code=401, detail="No se pudo autenticar con Siigo API : {str(e)}")
            time.sleep(RETRY_DELAY)
    
def create_siigo_customer(customer_data, token):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Partner-Id": os.getenv('SIIGO_PARTNER_ID'),
        "idempotency-key": str(uuid.uuid4())  #genera una key unica para cada solicitud
    }
    for attempt in range(MAX_REQUESTS_PER_SECOND):
        try:
            response = requests.post(f"{SIIGO_API_URL}/customers", json=customer_data, headers=headers)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:  #limite de solicitudes excedido
                retry_after = int(e.response.headers.get("Retry-After", RETRY_DELAY))
                time.sleep(retry_after)
            elif e.response.status_code in[400, 401, 403, 404, 500]:
                error_data = e.response.json()
                raise HTTPException(status_code=e.response.status_code, detail=f"Error de Siigo API: {error_data.get('message', str(e))}")
            else:
                time.sleep(RETRY_DELAY)

def check_customer_exists(identification, token):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Partner-Id": os.getenv('SIIGO_PARTNER_ID')
    }
    params = {
        "identification": identification
    }
    for attempt in range(MAX_REQUESTS_PER_SECOND):
        try:
            response = requests.get(f"{SIIGO_API_URL}/customers", headers=headers, params=params)
            response.raise_for_status()
            customers = response.json()
            return len(customers) > 0
        except requests.exceptions.RequestException as e:
            if attempt == MAX_REQUESTS_PER_SECOND - 1:
                raise HTTPException(status_code=500, detail=f"Error al verificar el cliente en Siigo: {str(e)}")
            time.sleep(RETRY_DELAY)
    
def send_email(to_email, subject, body):
    smtp_server = os.getenv('SMTP_SERVER')
    smtp_port = int(os.getenv('SMTP_PORT'))
    smtp_username = os.getenv('SMTP_USERNAME')
    smtp_password = os.getenv('SMTP_PASSWORD')


    msg = MIMEMultipart()
    msg['From'] = smtp_username
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_username, smtp_password)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"Error al enviar el correo: {str(e)}")
        return False

def add_to_sheet(user: UserRegistration):
    sheet.append_row([user.first_name, user.last_name, user.email, user.phone, user.identification, user.address, user.city])

def process_registration(user: UserRegistration):
    token = get_siigo_token()
    
    if check_customer_exists(user.identification, token):
        return {"message": "El usuario ya está registrado", "status": "existing"}
    
    customer_data = {
        "type": "Customer",
        "person_type": "Person",
        "id_type": "13",
        "identification": user.identification,
        "name": [user.first_name, user.last_name],
        "commercial_name": f"{user.first_name} {user.last_name}",
        "email": user.email,
        "phone": user.phone,
        "address": user.address,
        "city": user.city
    }
    siigo_customer = create_siigo_customer(customer_data, token)
    
    add_to_sheet(user)
    
    email_subject = "Bienvenido a TIMBALE\nAquí inicia tu viaje donde tu conciencia toma sentido humano y valor Personal"
    email_body = f"Hola {user.first_name},\n\nTu cuenta ha sido creada exitosamente. Tu ID de cliente es {siigo_customer['id']}."
    email_sent = send_email(user.email, email_subject, email_body)
    
    if email_sent:
        return {"message": "Usuario Registrado en la plataforma de Facturación y correo enviado exitosamente", "siigo_customer_id": siigo_customer['id'], "status": "new"}
    else:
        return {"message": "Usuario Registrado, pero hubo un problema al enviar el correo", "siigo_customer_id": siigo_customer['id'], "status": "new"}

@app.post("/register")
async def register_user(user: UserRegistration, background_tasks: BackgroundTasks):
    async with httpx.AsyncClient() as client:
        try:
            result = await process_registration(user, client)
            return result
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    
async def process_registration(user: UserRegistration, client: httpx.AsyncClient):
    token = get_siigo_token()

    if check_customer_exists(user.identification, token, client):
        return {"message": "El usuario ya está registrado", "status": "existing"}
    
    #customer_data = {
    #    "type": "Customer",
    #    "person_type": "Person",
    #    "id_type": "13",
    #    "identification": user.identification,
    #    "name": [user.first_name, user.last_name],
    #    "commercial_name": f"{user.first_name} {user.last_name}",
    #   "email": user.email,
    #    "phone": user.phone,
    #    "address": user.address,
    #    "city": user.city
    # } 
    # siigo_customer = await create_siigo_customer(customer_data, token, client)
    #
    # add_to_sheet(user)
    
async def check_customer_exists(identification, token, client: httpx.AsyncClient):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Partner-Id": os.getenv('SIIGO_PARTNER_ID')
    }
    params = {
        "identification": identification
    }  
    async with client.get(f"{SIIGO_API_URL}/customers", headers=headers, params=params) as response:
        response.raise_for_status()
        customers = response.json()
        return len(customers) > 0
    
@app.post("/register-from-timbale")
async def register_from_timbale(user_data: UserRegistration, background_tasks: BackgroundTasks):
    try:
        # Procesar los datos del formulario
        result = process_registration(user_data)

        # Enviar mensaje de WhatsApp
        send_whatsapp_message(user_data.phone, f"Hola {user_data.first_name}, Estamos Felices de que ahora haces Parte de la Famili Timbale, Tu registro fue exitoso.")

        return result
    except SiigoAPIError as e:
        raise HTTPException(status_code=500, detail=f"Error al crear el cliente en Siigo: {str(e)}")
    except EmailError as e:
        raise HTTPException(status_code=500, detail=f"Error al enviar el correo electrónico: {str(e)}")

@app.post("/process-sheet")
async def process_sheet(background_tasks: BackgroundTasks):
    values = sheet.get_all_values()
    
    for row in values[1:]:
        user = UserRegistration(
            first_name=row[0],
            last_name=row[1],
            email=row[2],
            phone=row[3],
            identification=row[4],
            address=row[5] if len(row) > 5 else "",
            city=row[6] if len(row) > 6 else ""
        )
        background_tasks.add_task(process_registration, user)
    
    return {"message": f"Procesando {len(values) - 1} entradas de la hoja"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port= int(os.getenv('PORT', 8000)))