#Componentes clave: Se utiliza FastAPI para crear un servidor web, 
# gspread para interactuar con Google Sheets, 
# requests para hacer solicitudes a la API de Siigo y smtplib para enviar correos electrónicos.

#Webhook: La forma más común de recibir datos de un formulario es a través de un webhook
#bibliotecas estandar de python
import json
import smtplib
from typing import Optional
import asyncio
import webbrowser
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import uuid
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import logging
import base64


#bibliotecas de terceros necesarias para el funcionamiento del codigo
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from pydantic import BaseModel
import gspread
#from google.oauth2.service_account import Credentials
#from oauth2client.service_account import ServiceAccountCredentials      validar despues
import httpx
#from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import uvicorn
from dotenv import load_dotenv

#bibliotecas propias del proyecto
from siigo_api import SiigoAPIError
from email_error import EmailAPIError
#from whatsapp import send_whatsapp_message



app = FastAPI()
load_dotenv()   #para importar y manejar las variables de entorno minimizando la exposicion de credenciales

#Parametros de configuracion de la API para Siigo (Restricciones de uso), accesos a la API, 
#manejo de errores y manejo de solicitudes
#manejo de variables de entorno con os.getenv


#logging.basicConfig(level=logging.INFO)
logging.basicConfig(level=logging.DEBUG)

def validate_env_vars():  #funcion para validar las variables de entorno
    required_vars = [
        'SIIGO_API_URL', 'SIIGO_API_TOKEN', 'SIIGO_PARTNER_ID', 'SIIGO_API_USERNAME',
        'GOOGLE_CREDS_PATH', 'SHEET_ID', 'SMTP_SERVER', 'SMTP_PORT',
        'SMTP_USERNAME', 'SMTP_PASSWORD'
    ]
     
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        raise ValueError(f"Faltan las siguientes variables de entorno: {', '.join(missing_vars)}")
    print("Todas las variables de entorno requeridas están configuradas.")

validate_env_vars()


SIIGO_API_URL = os.getenv('SIIGO_API_URL', "https://api.siigo.com")
SIIGO_AUTH_URL = f"{SIIGO_API_URL}/auth"
MAX_RETRIES = 3
RETRY_DELAY = 1


def create_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Partner-Id": os.getenv('SIIGO_PARTNER_ID')  # Asegúrate de tener el Partner-ID en tus variables de entorno
    
    }


# Google Sheets setup
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
# Carga las credenciales desde el archivo JSON de la cuenta de servicio para la autenticacion automatica de google
creds = Credentials.from_service_account_file(
    os.getenv('GOOGLE_CREDS_PATH'),
    scopes=SCOPES
    )

#crea los clientes de Google Sheets y Gmail
sheets_service = build('sheets', 'v4', credentials=creds)
gmail_service = build('gmail', 'v1', credentials=creds)
lock= asyncio.Lock()



#  Modelo Pydantic para datos de registro de usuario
class UserRegistration(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone: str
    identification: str
    address: Optional[str] = ""
    city: Optional[str] = ""

#Manejo de ERRORES de la API
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "code": exc.status_code}
        )

#funcion para leer los datos de la hoja de calculo  de Google Sheets
async def read_sheet_data(range: str = 'A1:AE100'):
    try:
        sheet = sheets_service.spreadsheets()
        result = sheet.values().get(spreadsheetId=os.getenv('SHEET_ID'), range=range).execute() 
        return result.get('values', [])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al leer los datos de la hoja de calculo: {str(e)}")
#funcion para obtener el token de acceso de Siigo basado en el token brindado por la gente de soporte de siigo
#manejo de solicitudes asincronas con httpx y manejo de errores con raise_for_status
#manejo de solicitudes asincronas con asyncio y manejo de solicitudes con httpx
#manejo de solicitudes asincronas con asyncio.Lock para garantizar la seguridad de subprocesos para operaciones SMTP
#manejo de solicitudes asincronas con asyncio.to_thread para ejecutar operaciones de forma asincrona
#implementa un mecanismo de reintentos para manejar errores de conexion y mantiene un registro de intentos y espera antes de lanzar una excepcion   
async def get_siigo_token(client: httpx.AsyncClient) -> str:
    headers = create_headers("")
    #token = base64.b64encode(f"{username}:{password}".encode()).decode()
    auth_data={
        "username": os.getenv('SIIGO_API_USERNAME'),  #usuario de prueba para la api de siigo 
        "password": os.getenv('SIIGO_API_PASSWORD')
         
    }
    logging.debug(f"Headers: {headers}")
    logging.debug(f"Auth data: {auth_data}")

    return await execute_with_retries(
       lambda: client.post(SIIGO_AUTH_URL, json=auth_data, headers=headers),
        error_message="No se pudo autenticar con Siigo API"
    )


async def execute_with_retries(request_func, retries: int = MAX_RETRIES, error_message: str = ""):
    #Ejecuta una función de solicitud con reintentos en caso de error.
    for attempt in range(retries):
        try:
            response = await request_func()
            logging.debug(f"Response status: {response.status_code}")
            logging.debug(f"Response content: {response.text}")
            response.raise_for_status()
            return response.json().get("access_token")
        except httpx.HTTPError as e:
            logging.error(f"Error en intento {attempt + 1}: {str(e)}")
            if attempt == retries - 1:
                raise HTTPException(status_code=401, detail=f"No se pudo autenticar con Siigo API, {error_message} : {str(e)}")
            await asyncio.sleep(RETRY_DELAY)

#Funcion para procesar los datos de la hoja de calculo de Google Sheets
async def process_sheet_data():
    async with httpx.AsyncClient() as client:
        rows = await read_sheet_data()     # Leer los datos de la hoja

        for row in rows:
            try:
                # Verificar que la fila tenga los campos necesarios
                if len(row) < 5:
                    print(f"Fila incompleta: {row}")
                    continue

                # Creacion de objeto UserRegistration con los datos de la fila
                user = UserRegistration(
                    first_name=row[0],
                    last_name=row[1],
                    email=row[2],
                    phone=row[3],
                    identification=row[4],
                    address=row[5] if len(row) > 5 else "",
                    city=row[6] if len(row) > 6 else ""
                )

                      #obtencion del token siigo
                token = await get_siigo_token(client)

                #validacion de existencia del cliente en Siigo
                if not await check_customer_exists(user.identification, token, client):
                        #si no existe, se crea el cliente en Siigo
                        siigo_response  = await register_user_in_siigo(user, client)

                        if siigo_response ['status'] == 'new':
                            # Enviar WhatsApp  por el momento desaactivado
                            #await send_whatsapp(user.phone, f"Hola {user.first_name}, bienvenido a TIMBALE. Tu cuenta ha sido creada exitosamente")
                            pass
                else:
                    print(f"El Usuario {user.identification} ya existe en Siigo.")
            except Exception as e:
                    print(f"Error procesando la fila  {row}: {str(e)}")

# Función para crear cliente en Siigo
async def create_siigo_customer(customer_data: dict, token: str, client: httpx.AsyncClient):
    headers = create_headers(token)
    headers["idempotency-key"] = str(uuid.uuid4())  # Agregar clave de idempotencia

    for attempt in range(MAX_RETRIES):
        try:
            response = await client.post(f"{SIIGO_API_URL}/customers", json=customer_data, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            if e.response.status_code == 429:  # Límite de solicitudes excedido
                retry_after = int(e.response.headers.get("Retry-After", RETRY_DELAY))
                await asyncio.sleep(retry_after)
            elif e.response.status_code in [400, 401, 403, 404, 500]:
                error_data = e.response.json()
                raise HTTPException(status_code=e.response.status_code, detail=f"Error de Siigo API: {error_data.get('message', str(e))}")
            else:
                if attempt == MAX_RETRIES - 1:
                    raise HTTPException(status_code=500, detail=f"Error al crear el cliente en Siigo después de {MAX_RETRIES} intentos")
                await asyncio.sleep(RETRY_DELAY)
    raise HTTPException(status_code=500, detail="Error al crear el cliente en Siigo después de {MAX_RETRIES} intentos")

            
#Funcion para verificar si el cliente ya existe en Siigo
async def check_customer_exists(identification: str, token: str, client: httpx.AsyncClient):
    headers = create_headers(token)
    params = {
        "identification": identification
    }
    try:
        response = await client.get(f"{SIIGO_API_URL}/customers", headers=headers, params=params)
        response.raise_for_status()
        customers = response.json().get('results', [])
        return len(customers) > 0
    except httpx.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"Error al verificar el cliente en Siigo: {str(e)}")


#Funcion para enviar mensajes de whatsapp
#async def send_whatsapp(phone: str, message: str):
    # Implementa aquí la lógica para enviar WhatsApp
#    print(f"Enviando WhatsApp a {phone}: {message}")

#funcion para procesar el registro de un usuario teniendo en cuenta la existencia de un cliente en Siigo y envio de correo de bienvenida. esta funcion se encarga de orquestar el proceso de registro y envio de correo de bienvenida
async def process_user_registration(user: UserRegistration, client: httpx.AsyncClient):
    try:
        # Registrar al usuario en Siigo
        result = await register_user_in_siigo(user, client)

        if result["status"] == "new":
            siigo_customer_id = result["siigo_customer_id"]

            # Enviar correo de bienvenida
            email_sent = await send_welcome_email(user, siigo_customer_id)

            if email_sent:
                print("Usuario registrado y correo enviado exitosamente.")
            else:
                print("Usuario registrado, pero no se pudo enviar el correo.")
        else:
            print(result["message"])

    except Exception as e:
        print(f"Error en el proceso de registro del usuario: {str(e)}")


#Funcion para enviar correos electronicos
async def send_email(to_email: str, subject: str, body:str) -> bool:
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
        async with asyncio.Lock: #garantizar la seguridad de subprocesos para operaciones SMTP
             with smtplib.SMTP(smtp_server, smtp_port) as server:
                 server.starttls()
                 server.login(smtp_username, smtp_password)
                 server.send_message(msg)
                 logging.info(f"Correo enviado exitosamente a {to_email}")
                 return True
             
    except smtplib.SMTPAuthenticationError:
        logging.error("Error de autenticación SMTP. Verifica tus credenciales.")
    except smtplib.SMTPException as e:
        logging.error(f"Error al enviar el correo: {str(e)}")
    except Exception as e:
        logging.error(f"Error inesperado: {str(e)}")
    return False

#Reemplacé los print() por logging, lo que facilita la depuración y captura de errores en producción.
#El uso del bloque de autenticación SMTP es más claro y se manejan errores específicos para evitar que fallos silenciosos pasen desapercibidos.







#maneja el flujo completo de registro de un usuario, incluyendo la verificación de existencia, creación en Siigo, adición a la hoja de cálculo y envío de correo electrónico.
#funcion para procesar el registro de un usuario

async def register_user_in_siigo(user: UserRegistration, client: httpx.AsyncClient) -> dict:
#obtencion del token siigo
    token = await get_siigo_token(client)
    # Verificar si el usuario ya está registrado
    if await check_customer_exists(user.identification, token, client):
        return {"message": "El usuario ya está registrado", "status": "existing"}
    
    #creacion de datos del cliente para enviar a Siigo
    customer_data = build_customer_data(user)

    # Registrar cliente en Siigo
    siigo_response = await create_siigo_customer(customer_data, token, client)

    return parse_siigo_response(siigo_response)
    
def build_customer_data(user: UserRegistration) -> dict:
#Preparacion de los datos para la creación del cliente en Siigo
    return {
        "type": "Customer",
        "person_type": "Person",
        "id_type": "13",
        "identification": user.identification,
        "name": [user.first_name, user.last_name],
        "commercial_name": f"{user.first_name} {user.last_name}",
        "email": user.email,
        "phone": user.phone,
        "address": user.address,
        "city": user.city,
    }

def parse_siigo_response(siigo_response: dict) -> dict:
        #Procesa la respuesta de Siigo y devuelve un resultado uniforme.
        if 'id' in siigo_response:  
            return {
                "message": "Usuario registrado exitosamente en Siigo.",
                "siigo_custumer_id": siigo_response['id'],
                "status": "new",
            }
        else:
            return{"message": "Error al registrar usuario en Siigo.", "status": "error"}

#funcion para enviar correo de bienvenida
async def send_welcome_email(user: UserRegistration, siigo_customer_id: str) -> bool:
    email_subject = (
        "Bienvenido a TIMBALE\nAquí inicia tu viaje donde tu conciencia "
        "toma sentido humano y valor Personal"
    )
    email_body = (
        f"Hola {user.first_name},\n\n"
        "Tu cuenta ha sido creada exitosamente.\n"
        f"Tu Id de Cliente es: {siigo_customer_id}."
    )

    # Llamar a la función de envío de correo
    email_sent = await send_email(user.email, email_subject, email_body)

    if email_sent:
        print(f"Correo enviado exitosamente a {user.email}")
        return True
    else:
        print(f"Error al enviar el correo a {user.email}")
        return False


#funcion para ejecutar el proceso de la hoja de calculo de Google Sheets cada 30 minutos
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Código de inicialización
    scheduler = AsyncIOScheduler()
    scheduler.add_job(process_sheet_data, 'interval', minutes=30)
    scheduler.start()
    
    yield  # Este yield es donde la aplicación se ejecuta
    
    # Código de limpieza (si es necesario)
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)
        

@app.post("/register-from-timbale")
async def register_from_timbale(user_data: UserRegistration, background_tasks: BackgroundTasks):
    try:
        async with httpx.AsyncClient() as client:
        # Procesar los datos del formulario
          result = await register_user_in_siigo(user_data, client)
          #ela linea siguiente es para nviar mensaje de whatsapp
          #background_tasks.add_task(send_whatsapp_message, user_data.phone, f"Hola {user_data.first_name}, Estamos Felices de que ahora haces Parte de la Famili Timbale, Tu registro fue exitoso.")
        
        return result
    except SiigoAPIError as e:
        raise HTTPException(status_code=500, detail=f"Error al crear el cliente en Siigo: {str(e)}")
    except EmailAPIError as e:
        raise HTTPException(status_code=500, detail=f"Error al enviar el correo electrónico: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error inesperado: {str(e)}")



@app.post("/process-sheet")
async def trigger_process_sheet():
    background_tasks= BackgroundTasks()
    background_tasks.add_task(process_sheet_data)
    return {"message": "Procesamiento de la hoja iniciado en segundo plano"}


    #async def process_sheet_data():
     #   sheet = sheets_service.spreadsheets()
      #  result = sheet.values().get(spreadsheetId=os.getenv('SHEET_ID'), range='A1:G1').execute()
       # values = result.get('values', [])
        #async with httpx.AsyncClient() as client:
         #   for row in values:
          #      if row in values:
           #         user = UserRegistration(
            #            first_name=row[0],
             #           last_name=row[1],
              #          email=row[2],
               #         phone=row[3],
                #        identification=row[4],
                 #       address=row[5] if len(row) > 5 else "",
                  #      city=row[6] if len(row) > 6 else ""
                #)
                #await process_registration(user, client)

   # background_tasks.add_task(process_sheet_data)
   # return {"message": "Procesamiento de la hoja iniciado en segundo plano"}

if __name__ == "__main__":
    port = int(os.getenv('PORT', 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

