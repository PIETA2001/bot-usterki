import os
import json
import logging
import io
import asyncio # <--- DODAJ TO
from datetime import datetime
from dotenv import load_dotenv

# --- Importy Bibliotek ---
import google.generativeai as genai
import gspread
import time
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload
from googleapiclient.errors import HttpError

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# --- 1. Konfiguracja Logowania ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 2. Ładowanie Kluczy API ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    logger.critical("BŁĄD: Nie znaleziono tokenów (TELEGRAM_TOKEN lub GEMINI_API_KEY) w pliku .env")
    exit()

# --- 3. KONFIGURACJA OAuth 2.0 (dla serwera) ---
GOOGLE_CREDENTIALS_FILE = 'credentials.json' 
GOOGLE_TOKEN_FILE = 'token.json' 
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']

GOOGLE_SHEET_NAME = 'Odbiory_Kolonia_Warszawska'
WORKSHEET_NAME = 'Arkusz1'
G_DRIVE_MAIN_FOLDER_NAME = 'Lokale' 

# Globalne obiekty API
gc = None
worksheet = None
drive_service = None
g_drive_main_folder_id = None 

def get_google_creds():
    """Wersja serwerowa: Wczytuje token.json i odświeża go w razie potrzeby."""
    creds = None
    
    if os.path.exists(GOOGLE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, SCOPES)
    else:
        logger.critical(f"BŁĄD KRYTYCZNY: Brak pliku {GOOGLE_TOKEN_FILE}!")
        exit() 

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Token wygasł, odświeżanie...")
            
            if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
                 logger.critical(f"BŁĄD KRYTYCZNY: Brak pliku {GOOGLE_CREDENTIALS_FILE}!")
                 exit()
                 
            try:
                creds.refresh(Request())
            except Exception as e:
                logger.critical(f"BŁĄD KRYTYCZNY: Nie można odświeżyć tokenu. Błąd: {e}")
                exit()
        else:
            logger.critical("BŁĄD KRYTYCZNY: Nie można odświeżyć tokenu (brak refresh_token).")
            exit()
    
    logger.info("Pomyślnie załadowano i zweryfikowano token Google (OAuth 2.0)")
    return creds

try:
    creds = get_google_creds()
    logger.info("Pomyślnie uzyskano dane logowania Google (OAuth 2.0)")

    gc = gspread.authorize(creds) 
    spreadsheet = gc.open(GOOGLE_SHEET_NAME)
    worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
    logger.info(f"Pomyślnie połączono z Arkuszem Google: {GOOGLE_SHEET_NAME}")

    drive_service = build('drive', 'v3', credentials=creds)
    logger.info("Pomyślnie połączono z Google Drive")

    logger.info(f"Szukanie folderu: '{G_DRIVE_MAIN_FOLDER_NAME}'...")
    response_folder = drive_service.files().list(
        q=f"name='{G_DRIVE_MAIN_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and 'root' in parents and trashed=False",
        spaces='drive',
        fields='files(id, name)',
    ).execute()
    
    files = response_folder.get('files', [])
    if not files:
        logger.critical(f"BŁĄD KRYTYCZNY: Nie znaleziono folderu '{G_DRIVE_MAIN_FOLDER_NAME}' na Twoim 'Mój Dysk'!")
        exit()
    
    g_drive_main_folder_id = files[0].get('id')
    logger.info(f"Pomyślnie znaleziono folder '{G_DRIVE_MAIN_FOLDER_NAME}' (ID: {g_drive_main_folder_id})")

except Exception as e:
    logger.critical(f"BŁĄD KRYTYCZNY: Nie można połączyć z Google: {e}")
    exit()


# --- 4. Konfiguracja Gemini (AI) ---
genai.configure(api_key=GEMINI_API_KEY)
generation_config = {
    "temperature": 0.2,
    "max_output_tokens": 2048,
    "response_mime_type": "application/json", 
}
model = genai.GenerativeModel(
    model_name="gemini-2.5-flash", 
    generation_config=generation_config
)

# --- 5. ZMIENIONY Prompt dla AI ---
PROMPT_SYSTEMOWY = """
Twoim zadaniem jest analiza zgłoszenia serwisowego. Przetwórz wiadomość użytkownika i wyekstrahuj DOKŁADNIE 3 informacje: numer_lokalu_budynku, rodzaj_usterki, podmiot_odpowiedzialny.

Zawsze odpowiadaj WYŁĄCZNIE w formacie JSON, zgodnie z tym schematem:
{
  "numer_lokalu_budynku": "string",
  "rodzaj_usterki": "string",
  "podmiot_odpowiedzialny": "string"
}

Ustalenia:
1.  numer_lokalu_budynku: (np. "15", "104B", "Budynek C, klatka 2", "Lokal 46/2")
2.  rodzaj_usterki: (np. "cieknący kran", "brak prądu", "winda nie działa", "porysowana szyba")
3.  podmiot_odpowiedzialny: (np. "administracja", "serwis", "deweloper", "domhomegroup", "Janusz Pelc"). WAŻNE: Jeśli podmiot wygląda jak imię i nazwisko (np. Jan Kowalski), potraktuj to jako poprawną nazwę firmy/podmiotu, a NIE "BRAK DANYCH".
4.  Jeśli jakiejś informacji (poza imionami i nazwiskami) brakuje, wstaw w jej miejsce "BRAK DANYCH".
5.  Jeśli wiadomość to 'Rozpoczęcie odbioru', potraktuj to jako 'rodzaj_usterki' jeśli nie ma innej usterki.
6.  Nigdy nie dodawaj żadnego tekstu przed ani po obiekcie JSON. Ani '```json' ani '```'.

Wiadomość użytkownika do analizy znajduje się poniżej.
"""

# --- 6. Funkcja do Zapisu w Arkuszu ---
def zapisz_w_arkuszu(dane_json: dict, data_telegram: datetime) -> bool:
    """Zapisuje przeanalizowane dane w nowym wierszu Arkusza Google."""
    try:
        data_str = data_telegram.strftime('%Y-%m-%d %H:%M:%S')
        nowy_wiersz = [
            data_str,
            dane_json.get('numer_lokalu_budynku', 'BŁĄD JSON'),
            dane_json.get('rodzaj_usterki', 'BŁĄD JSON'),
            dane_json.get('podmiot_odpowiedzialny', 'BŁĄD JSON')
        ]
        worksheet.append_row(nowy_wiersz, value_input_option='USER_ENTERED')
        logger.info(f"Dodano wiersz do arkusza: {nowy_wiersz}")
        return True
    except Exception as e:
        logger.error(f"Błąd podczas zapisu do Google Sheets: {e}")
        return False

# --- ZMIENIONA FUNKCJA WYSYŁANIA NA GOOGLE DRIVE ---
def upload_photo_to_drive(file_bytes, lokal_name, usterka_name, podmiot_name):
    """
    Wyszukuje podfolder lokalu i wysyła do niego zdjęcie.
    ZWRACA: (success, message, file_id)
    """
    global drive_service, g_drive_main_folder_id
    
    try:
        q_str = f"name='{lokal_name}' and mimeType='application/vnd.google-apps.folder' and '{g_drive_main_folder_id}' in parents and trashed=False"
        response = drive_service.files().list(q=q_str, spaces='drive', fields='files(id, name)').execute()
        lokal_folder = response.get('files', [])

        if not lokal_folder:
            logger.error(f"Nie znaleziono folderu dla lokalu: {lokal_name} wewnątrz '{G_DRIVE_MAIN_FOLDER_NAME}'")
            return False, f"Nie znaleziono folderu Drive dla '{lokal_name}'", None

        lokal_folder_id = lokal_folder[0].get('id')
        file_name = f"{usterka_name} - {podmiot_name}.jpg"
        file_metadata = {'name': file_name, 'parents': [lokal_folder_id]}
        
        file_bytes.seek(0)
        media = MediaIoBaseUpload(file_bytes, mimetype='image/jpeg', resumable=True)
        
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, name', # Prosimy o 'name' i 'id' w odpowiedzi
        ).execute()
        
        file_id = file.get('id')
        file_name_created = file.get('name')
        logger.info(f"Pomyślnie wysłano plik '{file_name_created}' do folderu '{lokal_name}' (ID: {file_id})")
        return True, file_name_created, file_id # Zwracamy ID pliku!
    
    except Exception as e:
        logger.error(f"Błąd podczas wysyłania na Google Drive: {e}")
        return False, str(e), None

# --- NOWA FUNKCJA DO USUWANIA Z GOOGLE DRIVE ---
def delete_file_from_drive(file_id: str) -> bool:
    """Usuwa plik z Google Drive na podstawie jego ID."""
    global drive_service
    if not file_id:
        logger.error("Próba usunięcia pliku, ale brak file_id.")
        return False
        
    try:
        drive_service.files().delete(fileId=file_id).execute()
        logger.info(f"Pomyślnie usunięto plik z Drive (ID: {file_id})")
        return True
    except HttpError as e:
        if e.resp.status == 404:
            logger.warning(f"Nie można usunąć pliku (ID: {file_id}), już nie istnieje.")
            return True # Traktujemy jako sukces, bo pliku i tak nie ma
        logger.error(f"Błąd podczas usuwania pliku z Drive (ID: {file_id}): {e}")
        return False
    except Exception as e:
        logger.error(f"Nieznany błąd podczas usuwania pliku z Drive (ID: {file_id}): {e}")
        return False


# --- NOWA FUNKCJA OBSŁUGI COFANIA ---
async def handle_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Obsługuje logikę cofania usterki (tekstowej lub zdjęcia)."""
    
    replied_message = update.message.reply_to_message
    replied_text = replied_message.text
    chat_data = context.chat_data
    
    # 1. Sprawdź, czy to cofnięcie USTERKI TEKSTOWEJ
    text_prefix = "➕ Dodano (tekst): '"
    if replied_text.startswith(text_prefix):
        try:
            # Wyodrębnij treść usterki spomiędzy '...'\n(Łącznie...
            text_suffix = "'\n(Łącznie:"
            start = len(text_prefix)
            end = replied_text.find(text_suffix, start)
            if end == -1: # Na wypadek gdyby coś się zmieniło w tekście
                raise ValueError("Nie znaleziono znacznika końca")
                
            usterka_to_remove = replied_text[start:end]
            
            if usterka_to_remove in chat_data.get('odbiur_usterki', []):
                chat_data['odbiur_usterki'].remove(usterka_to_remove)
                logger.info(f"Cofnięto (tekst): {usterka_to_remove}")
                await update.message.reply_text(
                    f"↩️ Cofnięto usterkę (tekst):\n'{usterka_to_remove}'\n\n"
                    f"(Łącznie: {len(chat_data['odbiur_usterki'])})."
                )
            else:
                logger.warning("Próbowano cofnąć tekst, którego nie ma na liście.")
                await update.message.reply_text("❌ Nie znaleziono tej usterki na liście (może już ją cofnąłeś).")
            return
            
        except Exception as e:
            logger.error(f"Błąd parsowania tekstu do cofnięcia: {e}")
            await update.message.reply_text("❌ Wystąpił błąd przy próbie cofnięcia tej usterki.")
            return

    # 2. Sprawdź, czy to cofnięcie ZDJĘCIA
    photo_prefix = "✅ Zdjęcie zapisane na Drive"
    if replied_text.startswith(photo_prefix):
        try:
            # A. Wyodrębnij ID pliku z ukrytego znacznika
            hidden_marker = " \u200B" # Spacja + Znak Zerowej Szerokości
            parts = replied_text.split(hidden_marker)
            if len(parts) != 3:
                raise ValueError("Brak ukrytego znacznika ID pliku w wiadomości.")
            
            file_id_to_delete = parts[1]
            
            # B. Wyodrębnij treść usterki (dla listy)
            content_prefix = "➕ Usterka dodana do listy: '"
            content_suffix = "'\n(Łącznie:"
            
            content_line_start = replied_text.find(content_prefix)
            if content_line_start == -1:
                 raise ValueError("Nie znaleziono linii 'Usterka dodana do listy'")
            
            start = content_line_start + len(content_prefix)
            end = replied_text.find(content_suffix, start)
            if end == -1:
                 raise ValueError("Nie znaleziono znacznika końca usterki zdjęcia")

            usterka_to_remove = replied_text[start:end] # np. "Rysa na szybie (zdjęcie)"

            # C. Wykonaj akcje
            if usterka_to_remove in chat_data.get('odbiur_usterki', []):
                # Usuń z listy
                chat_data['odbiur_usterki'].remove(usterka_to_remove)
                logger.info(f"Cofnięto (z listy): {usterka_to_remove}")
                
                # Usuń z Drive
                if delete_file_from_drive(file_id_to_delete):
                    await update.message.reply_text(
                        f"↩️ Cofnięto usterkę (tekst ORAZ zdjęcie z Drive):\n'{usterka_to_remove}'\n\n"
                        f"(Łącznie: {len(chat_data['odbiur_usterki'])})."
                    )
                else:
                    logger.error(f"Krytyczny błąd: Usunięto '{usterka_to_remove}' z listy, ale NIE udało się usunąć pliku {file_id_to_delete} z Drive.")
                    await update.message.reply_text(
                        f"❌ BŁĄD KRYTYCZNY:\nUsunięto wpis z listy, ale NIE udało się usunąć pliku z Google Drive.\n"
                        f"Zgłoś to administratorowi (ID pliku: {file_id_to_delete})."
                    )
            else:
                logger.warning("Próbowano cofnąć zdjęcie, którego nie ma na liście.")
                await update.message.reply_text("❌ Nie znaleziono tej usterki na liście (może już ją cofnąłeś).")
            return

        except Exception as e:
            logger.error(f"Błąd parsowania zdjęcia do cofnięcia: {e}")
            await update.message.reply_text("❌ Wystąpił błąd przy próbie cofnięcia tego zdjęcia.")
            return

    # 3. Jeśli odpowiedziano na inną wiadomość
    await update.message.reply_text(
        "Nie można cofnąć tej wiadomości. \n"
        "Aby cofnąć, odpowiedz 'cofnij' bezpośrednio na wiadomość bota (tę z zielonym '✅' lub '➕')."
    )


# --- 7. ZMIENIONY Główny Handler (z logiką cofania) ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Przechwytuje wiadomość, sprawdza stan sesji i decyduje co robić."""
    
    if not update.message or (not update.message.text and not update.message.caption):
         logger.warning("Otrzymano pustą wiadomość (np. naklejkę). Ignorowanie.")
         return

    user_message = update.message.text
    if not user_message:
        if update.message.caption:
            logger.info("Wiadomość tekstowa jest pusta, ale jest caption. Przekazuję do handle_photo.")
            return
        else:
            logger.warning("Otrzymano wiadomość bez tekstu i bez caption. Ignorowanie.")
            return

    message_time = update.message.date
    chat_data = context.chat_data 

    # --- NOWA LOGIKA: SPRAWDŹ CZY TO POLECENIE COFNIĘCIA ---
    if user_message.lower().strip() == 'cofnij' and update.message.reply_to_message:
        if chat_data.get('odbiur_aktywny'):
            logger.info("Wykryto polecenie 'cofnij' w aktywnej sesji.")
            await handle_undo(update, context) # Przekaż do nowej funkcji
            return # Zakończ przetwarzanie tej wiadomości
        else:
            await update.message.reply_text("Żaden odbiór nie jest aktywny. Nie można nic cofnąć.")
            return
    # --- KONIEC LOGIKI COFANIA ---

    try:
        # --- LOGIKA SESJI ODBIORU ---

        # SCENARIUSZ 1: Użytkownik KOŃCZY odbiór
        if user_message.lower().strip() == 'koniec odbioru':
            if chat_data.get('odbiur_aktywny'):
                lokal = chat_data.get('odbiur_lokal')
                podmiot = chat_data.get('odbiur_podmiot')
                usterki_lista = chat_data.get('odbiur_usterki', [])
                
                if not usterki_lista:
                    await update.message.reply_text(f"Zakończono odbiór dla lokalu {lokal}. Nie dodano żadnych usterek.")
                else:
                    logger.info(f"Zapisywanie {len(usterki_lista)} usterek dla lokalu {lokal}...")
                    licznik_zapisanych = 0
                    for usterka in usterki_lista:
                        dane_json = {
                            "numer_lokalu_budynku": lokal,
                            "rodzaj_usterki": usterka,
                            "podmiot_odpowiedzialny": podmiot
                        }
                        if zapisz_w_arkuszu(dane_json, message_time): 
                            licznik_zapisanych += 1
                    
                    await update.message.reply_text(f"✅ Zakończono odbiór.\nZapisano {licznik_zapisanych} z {len(usterki_lista)} usterek dla lokalu {lokal}.")
                
                chat_data.clear() 
            else:
                await update.message.reply_text("Żaden odbiór nie jest aktywny. Aby zakończyć, musisz najpierw go rozpocząć.")
            return 

        # SCENARIUSZ 2: Użytkownik ZACZYNA odbiór
        if user_message.lower().startswith('rozpoczęcie odbioru'):
            logger.info("Wykryto 'Rozpoczęcie odbioru', wysyłanie do Gemini po dane sesji...")
            await update.message.reply_text("Rozpoczynam odbiór... 🧠 Analizuję dane lokalu i firmy...")
            
            response = model.generate_content([PROMPT_SYSTEMOWY, user_message])
            cleaned_text = response.text.strip().replace("```json", "").replace("```", "").strip()
            dane_startowe = json.loads(cleaned_text)
            
            lokal = dane_startowe.get('numer_lokalu_budynku')
            podmiot = dane_startowe.get('podmiot_odpowiedzialny')

            if lokal == "BRAK DANYCH" or podmiot == "BRAK DANYCH":
                 await update.message.reply_text(f"❌ Nie udało się rozpoznać lokalu lub firmy (Lokal: {lokal}, Firma: {podmiot}).\nSpróbuj ponownie, np: 'Rozpoczęcie odbioru, lokal 46/2, firma Janusz Pelc'.")
            else:
                lokal_normalized = lokal.lower().replace("lokal", "").strip().replace("/", ".")
                
                chat_data['odbiur_aktywny'] = True
                chat_data['odbiur_lokal'] = lokal_normalized 
                chat_data['odbiur_podmiot'] = podmiot
                chat_data['odbiur_usterki'] = [] 
                await update.message.reply_text(f"✅ Rozpoczęto odbiór dla:\n\nLokal: {lokal_normalized}\nFirma: {podmiot}\n\nTeraz wpisuj usterki (tekst lub zdjęcia z opisem). Zakończ pisząc 'Koniec odbioru'.")
            
            return 

        # SCENARIUSZ 3: Odbiór jest AKTYWNY, a to jest usterka TEKSTOWA
        if chat_data.get('odbiur_aktywny'):
            logger.info(f"Odbiór aktywny. Wysyłanie usterki '{user_message}' do Gemini w celu ekstrakcji...")
            
            response = model.generate_content([PROMPT_SYSTEMOWY, user_message])
            cleaned_text = response.text.strip().replace("```json", "").replace("```", "").strip()
            dane_usterki = json.loads(cleaned_text)
            
            usterka_opis = dane_usterki.get('rodzaj_usterki', user_message) 
            if usterka_opis == "BRAK DANYCH":
                usterka_opis = user_message 
                
            chat_data['odbiur_usterki'].append(usterka_opis)
            
            # Ważne: Zapisujemy wiadomość, którą wysyłamy, aby móc na nią odpowiedzieć
            await update.message.reply_text(
                f"➕ Dodano (tekst): '{usterka_opis}'\n"
                f"(Łącznie: {len(chat_data['odbiur_usterki'])}). Wpisz kolejną lub 'Koniec odbioru'."
            )
            return
            
        # SCENARIUSZ 4: Wiadomość poza sesją
        else:
            logger.warning(f"Otrzymano wiadomość '{user_message}', gdy sesja nie jest aktywna. Ignorowanie.")
            await update.message.reply_text(
                "Żaden odbiór nie jest aktywny. \n"
                "Aby rozpocząć, napisz: 'Rozpoczęcie odbioru, [lokal], [firma]'.")
            return

    except json.JSONDecodeError as json_err:
        cleaned_text = locals().get('cleaned_text', 'BRAK DANYCH')
        logger.error(f"Błąd parsowania JSON od Gemini (w logice sesji): {json_err}. Odpowiedź AI: {cleaned_text}")
        await update.message.reply_text("❌ Błąd analizy AI. Spróbuj sformułować wiadomość inaczej.")
        return
    except Exception as session_err:
        logger.error(f"Wystąpił nieoczekiwany błąd w logice sesji: {session_err}")
        await update.message.reply_text(f"❌ Wystąpił krytyczny błąd: {session_err}")
        return


# --- 7b. ZMIENIONY Handler Zdjęć (dodaje ukryte ID) ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Przechwytuje zdjęcie W TRAKCIE aktywnej sesji odbioru."""
    chat_data = context.chat_data
    
    if not chat_data.get('odbiur_aktywny'):
        await update.message.reply_text("Wyślij zdjęcie *tylko po* rozpoczęciu odbioru. Teraz ta fotka zostanie zignorowana.")
        return

    usterka = update.message.caption
    if not usterka:
        await update.message.reply_text("❌ Zdjęcie musi mieć opis (usterkę)!\nInaczej nie wiem, co zapisać. Wyślij ponownie z opisem.")
        return

    lokal = chat_data.get('odbiur_lokal')
    podmiot = chat_data.get('odbiur_podmiot')
    
    await update.message.reply_text(f"Otrzymano zdjęcie dla usterki: '{usterka}'. Przetwarzam i wysyłam na Drive...")

    try:
        photo_file = await update.message.photo[-1].get_file()
        file_bytes_io = io.BytesIO()
        await photo_file.download_to_memory(file_bytes_io)
        
        # Odbieramy teraz 3 wartości, w tym ID pliku!
        success, message, file_id = upload_photo_to_drive(file_bytes_io, lokal, usterka, podmiot)
        
        if success:
            usterka_z_dopiskiem = f"{usterka} (zdjęcie)"
            chat_data['odbiur_usterki'].append(usterka_z_dopiskiem)
            
            # --- NOWA WIADOMOŚĆ Z UKRYTYM ZNACZNIKIEM ---
            hidden_marker = " \u200B" # Spacja + Znak Zerowej Szerokości
            
            reply_text = (
                f"✅ Zdjęcie zapisane na Drive jako: '{message}'\n"
                f"➕ Usterka dodana do listy: '{usterka_z_dopiskiem}'\n"
                f"(Łącznie: {len(chat_data['odbiur_usterki'])})."
                f"{hidden_marker}{file_id}{hidden_marker}" # Ukryte ID pliku na końcu
            )
            
            await update.message.reply_text(reply_text)
            # --- KONIEC NOWEJ WIADOMOŚCI ---
            
        else:
            await update.message.reply_text(f"❌ Błąd Google Drive: {message}")
            
    except Exception as e:
        logger.error(f"Błąd podczas przetwarzania zdjęcia: {e}")
        await update.message.reply_text(f"❌ Wystąpił błąd przy pobieraniu zdjęcia: {e}")


# --- 8. Uruchomienie Bota (WERSJA OSTATECZNA z context manager) ---

# Pobierz port z otoczenia (wymagane przez Cloud Run/Render)
PORT = int(os.getenv('PORT', 8080)) 

# Pobierz nasz publiczny URL ze zmiennej środowiskowej
# Pamiętaj, aby ustawić to na Render! Np. https://bot-usterki.onrender.com
WEBHOOK_URL = os.getenv('WEBHOOK_URL', "https://bot-usterki.onrender.com") 

async def main():
    """Główna funkcja uruchamiająca bota w trybie Webhook."""
    
    if not WEBHOOK_URL:
        logger.critical("BŁĄD: Zmienna środowiskowa 'WEBHOOK_URL' nie jest ustawiona!")
        return

    logger.info("Uruchamianie bota w trybie Webhook...")
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Dodaj handlery
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Użyj context managera. On automatycznie obsłuży
    # application.initialize() i application.shutdown()
    # To powinno poprawnie obsłużyć sygnały zamknięcia (np. z Render)
    async with application:
        try:
            await application.bot.set_webhook(
                url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}",
                allowed_updates=Update.ALL_TYPES
            )
            logger.info(f"Webhook ustawiony na adres: {WEBHOOK_URL}")
        except Exception as e:
            logger.error(f"BŁĄD KRYTYCZNY: Nie można ustawić webhooka: {e}")
            return # Zakończ, jeśli się nie udało

        # Uruchom serwer webhooka
        logger.info(f"Bot nasłuchuje na porcie {PORT} pod adresem 0.0.0.0")
        await application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            secret_token=TELEGRAM_TOKEN,
            webhook_url=WEBHOOK_URL
        )
        # Pętla będzie tu czekać na zawsze, aż dostanie sygnał stop


if __name__ == '__main__':
    # Ten blok jest teraz super prosty i poprawny.
    # asyncio.run() uruchomi main() i poprawnie obsłuży
    # zamknięcie pętli, gdy main() się zakończy (co jest
    # obsługiwane przez context manager 'async with application')
    try:
        asyncio.run(main())
    except RuntimeError as e:
        if "Cannot close a running event loop" in str(e):
            logger.warning("Znany błąd asyncio, ale bot powinien działać. Ignorowanie.")
        else:
            logger.critical(f"Aplikacja zatrzymana przez błąd: {e}")
    except Exception as e:
        logger.critical(f"Nieznany błąd krytyczny: {e}")


