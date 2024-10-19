#Componentes clave: Se utiliza FastAPI para crear un servidor web, 
# gspread para interactuar con Google Sheets, 
# requests para hacer solicitudes a la API de Siigo y smtplib para enviar correos electrónicos.

#Webhook: La forma más común de recibir datos de un formulario es a través de un webhook
#bibliotecas estandar de python
import json
import smtplib
from typing import Optional
import asyncio

#bibliotecas de terceros necesarias para el funcionamiento del codigo
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import httpx
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import uvicorn
import os
import time
import uuid
from dotenv import load_dotenv


#bibliotecas propias del proyecto
from siigo_api import SiigoAPIError
from email_error import EmailError
from whatsapp import send_whatsapp_message



app = FastAPI()
load_dotenv()   #para manejar las variables de entorno minimizando la exposicion de credenciales

#Parametros de configuracion de la API para Siigo (Restricciones de uso), accesos a la API, 
#manejo de errores y manejo de solicitudes
#manejo de variables de entorno con os.getenv
SIIGO_API_URL = os.getenv('SIIGO_API_URL', "https://api.siigo.com")
SIIGO_AUTH_URL = f"{SIIGO_API_URL}/auth"
MAX_RETRIES = 3
RETRY_DELAY = 1

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
creds = ServiceAccountCredentials.from_json_keyfile_name(os.getenv('GOOGLE_CREDS_PATH'), SCOPES)
client = gspread.authorize(creds)
sheet = client.open(os.getenv('SHEET_NAME')).worksheet(os.getenv('WORKSHEET_NAME'))

# Gmail API setup
GMAIL_CREDS = Credentials.from_authorized_user_file(os.getenv('GMAIL_CREDS_PATH'), ['https://www.googleapis.com/auth/gmail.send'])
gmail_service = build('gmail', 'v1', credentials=GMAIL_CREDS)

#manejo de errores de la API
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "code": exc.status_code}
        )

#funcion para obtener el token de acceso de Siigo basado en el token brindado por la gente de soporte de siigo
#manejo de solicitudes asincronas con httpx y manejo de errores con raise_for_status
#manejo de solicitudes asincronas con asyncio y manejo de solicitudes con httpx
#manejo de solicitudes asincronas con asyncio.Lock para garantizar la seguridad de subprocesos para operaciones SMTP
#manejo de solicitudes asincronas con asyncio.to_thread para ejecutar operaciones de forma asincrona
#implementa un mecanismo de reintentos para manejar errores de conexion y mantiene un registro de intentos y espera antes de lanzar una excepcion   
async def get_siigo_token(client: httpx.AsyncClient):
    headers = {
        "Authorization": f"Basic {os.getenv('SIIGO_API_TOKEN')}",
        "Content-Type": "application/json",
        "Partner-Id": os.getenv('SIIGO_PARTNER_ID')
    }
    auth_data={
        "username": os.getenv('SIIGO_API_USERNAME')  #usuario de prueba para la api de siigo 
    }
    for attempt in range(MAX_RETRIES):
        try:
            response = await client.post(SIIGO_AUTH_URL, json=auth_data, headers=headers)
            response.raise_for_status()
            return response.json()["access_token"]
        except httpx.HTTPError as e:
            if attempt == MAX_RETRIES - 1:
                raise HTTPException(status_code=401, detail="No se pudo autenticar con Siigo API : {str(e)}")
            await asyncio.sleep(RETRY_DELAY)
    

#Se utiliza una clave de idempotencia, lo cual es una buena práctica para evitar duplicados en caso de reintentos
async def create_siigo_customer(customer_data: dict, token: str, client: httpx.AsyncClient):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Partner-Id": os.getenv('SIIGO_PARTNER_ID'),
        "idempotency-key": str(uuid.uuid4())  #genera una key unica para cada solicitud
    }
    for attempt in range(MAX_RETRIES):
        try:
            response = await client.post(f"{SIIGO_API_URL}/customers", json=customer_data, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            if e.response.status_code == 429:  #limite de solicitudes excedido
                retry_after = int(e.response.headers.get("Retry-After", RETRY_DELAY))
                await asyncio.sleep(retry_after)
            elif e.response.status_code in[400, 401, 403, 404, 500]:
                error_data = e.response.json()
                raise HTTPException(status_code=e.response.status_code, detail=f"Error de Siigo API: {error_data.get('message', str(e))}")
            else:
                if attempt == MAX_RETRIES - 1:
                    raise HTTPException(status_code=500, detail=f"Error al crear el cliente en Siigo despues de {MAX_RETRIES} intentos:")
                await asyncio.sleep(RETRY_DELAY)
    raise HTTPException(status_code=500, detail="Error al crear el cliente en Siigo despues de {MAX_RETRIES} intentos")
            
async def check_customer_exists(identification: str, token: str, client: httpx.AsyncClient):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Partner-Id": os.getenv('SIIGO_PARTNER_ID')
    }
    params = {
        "identification": identification
    }
    try:
        response = await client.get(f"{SIIGO_API_URL}/customers", headers=headers, params=params)
        response.raise_for_status()
        customers = response.json()
        return len(customers) > 0
    except httpx.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"Error al verificar el cliente en Siigo: {str(e)}")
        
async def send_email(to_email: str, subject: str, body:str):
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
        async with asyncio.Lock(): #garantizar la seguridad de subprocesos para operaciones SMTP
             with smtplib.SMTP(smtp_server, smtp_port) as server:
                 server.starttls()
                 server.login(smtp_username, smtp_password)
                 server.send_message(msg)
                 return True
    except smtplib.SMTPException as e:
        print(f"Error SMTPal enviar el correo: {str(e)}")
        return False
    except Exception as e:
        print(f"Error inesperado al enviar el correo: {str(e)}")
        return False

async def add_to_sheet(user: UserRegistration):
    await asyncio.to_thread(sheet.append_row, [user.first_name, user.last_name, user.email, user.phone, user.identification, user.address, user.city])


#maneja el flujo completo de registro de un usuario, incluyendo la verificación de existencia, creación en Siigo, adición a la hoja de cálculo y envío de correo electrónico.
async def process_registration(user: UserRegistration, client: httpx.AsyncClient):
    try:
        token = await get_siigo_token(client)

        if await check_customer_exists(user.identification, token, client):
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
        siigo_customer = await create_siigo_customer(customer_data, token, client)
        await add_to_sheet(user)
        email_subject = "Bienvenido a TIMBALE\nAquí inicia tu viaje donde tu conciencia toma sentido humano y valor Personal"
        email_body = f"Hola {user.first_name},\n\nTu cuenta ha sido creada exitosamente."
        
        if 'id' in siigo_customer:
            email_body += f"\nTu Id de Cliente es: {siigo_customer['id']}."
            
            email_sent = await send_email(user.email, email_subject, email_body)
            
            if email_sent:
                return {"message": "Usuario Registrado en la plataforma de Facturación y correo enviado exitosamente", 
                "siigo_customer_id": siigo_customer.get('id'),
                "status": "new"
                }
            else:
                return {"message": "Usuario Registrado, pero hubo un problema al enviar el correo",
                "siigo_customer_id": siigo_customer.get('id'),
                "status": "new"
                }
            
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en el proceso de resgistro: {str(e)}")


@app.post("/register")
async def register_user(user: UserRegistration, background_tasks: BackgroundTasks):
    async with httpx.AsyncClient() as client:
        try:
            result = await process_registration(user, client)
            return result
        except HTTPException as e:
            raise e
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error Inesperado: {str(e)}")
        


@app.post("/register-from-timbale")
async def register_from_timbale(user_data: UserRegistration, background_tasks: BackgroundTasks):
    try:
        async with httpx.AsyncClient() as client:
        # Procesar los datos del formulario
          result = await process_registration(user_data, client)
          background_tasks.add_task(send_whatsapp_message, user_data.phone, f"Hola {user_data.first_name}, Estamos Felices de que ahora haces Parte de la Famili Timbale, Tu registro fue exitoso.")
        
        return result
    except SiigoAPIError as e:
        raise HTTPException(status_code=500, detail=f"Error al crear el cliente en Siigo: {str(e)}")
    except EmailError as e:
        raise HTTPException(status_code=500, detail=f"Error al enviar el correo electrónico: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error inesperado: {str(e)}")


@app.post("/process-sheet")
async def process_sheet(background_tasks: BackgroundTasks):
    async def process_sheet_data():
        values = await asyncio.to_thread(sheet.get_all_values)
        async with httpx.AsyncClient() as client:
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
                await process_registration(user, client)

    background_tasks.add_task(process_sheet_data)
    return {"message": "Procesamiento de la hoja iniciado en segundo plano"}

if __name__ == "__main__":
    port = int(os.getenv('PORT', 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)