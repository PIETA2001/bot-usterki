import os
import json
import logging
import io
from datetime import datetime
from dotenv import load_dotenv

# --- Importy Bibliotek ---
import google.generativeai as genai
import gspread
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# --- 1. Konfiguracja Logowania (Ważne do debugowania) ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 2. Ładowanie Kluczy API (z pliku .env) ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    logger.critical("BŁĄD: Nie znaleziono tokenów (TELEGRAM_TOKEN lub GEMINI_API_KEY) w pliku .env")
    exit()

# --- 3. NOWA KONFIGURACJA (OAuth 2.0 zamiast Service Account) ---
# Plik pobrany z Google Cloud Console (dla "Aplikacji komputerowej")
GOOGLE_CREDENTIALS_FILE = 'credentials.json' 
# Plik, który zostanie wygenerowany po pierwszej autoryzacji
GOOGLE_TOKEN_FILE = 'token.json' 

# Potrzebujemy uprawnień do Arkuszy i Dysku
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']

GOOGLE_SHEET_NAME = 'Odbiory_Kolonia_Warszawska'
WORKSHEET_NAME = 'Arkusz1'
G_DRIVE_MAIN_FOLDER_NAME = 'Lokale' 

# Globalne obiekty API
gc = None
worksheet = None
drive_service = None
g_drive_main_folder_id = None # ID folderu 'Lokale'

def get_google_creds():
    """
    Wersja serwerowa: Wczytuje token.json i odświeża go w razie potrzeby.
    NIE próbuje uruchamiać serwera lokalnego.
    """
    creds = None
    
    # Plik token.json MUSI istnieć na serwerze (wgrany jako Secret File)
    if os.path.exists(GOOGLE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, SCOPES)
    else:
        logger.critical(f"BŁĄD KRYTYCZNY: Brak pliku {GOOGLE_TOKEN_FILE}!")
        logger.critical("Wgraj 'token.json' wygenerowany lokalnie jako Secret File na serwerze.")
        exit() # Zatrzymuje bota, jeśli nie ma tokenu

    # Sprawdź, czy token jest ważny. Jeśli nie, spróbuj odświeżyć.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Token wygasł, odświeżanie...")
            
            # Odświeżenie tokenu w pamięci. 
            # Działa, o ile w pliku token.json jest "refresh_token", 
            # a plik credentials.json jest dostępny (też go wgramy).
            creds.refresh(Request()) 
            
            # NIE próbujemy zapisywać nowego tokenu, 
            # bo system plików serwera jest zwykle tylko do odczytu.
            # Odświeżenie w pamięci wystarczy do czasu restartu serwera.
        else:
            # Jeśli nie ma tokenu LUB nie ma refresh_tokena (plik jest uszkodzony/stary)
            logger.critical("BŁĄD KRYTYCZNY: Nie można odświeżyć tokenu.")
            logger.critical("Wygeneruj 'token.json' od nowa lokalnie i wgraj go na serwer.")
            exit()
    
    logger.info("Pomyślnie załadowano i zweryfikowano token Google (OAuth 2.0)")
    return creds

try:
    # --- 3a. Pobranie danych logowania (OAuth) ---
    creds = get_google_creds()
    logger.info("Pomyślnie uzyskano dane logowania Google (OAuth 2.0)")

    # --- 3b. Konfiguracja Google Sheets (gspread) ---
    # Używamy gspread.authorize() zamiast service_account()
    gc = gspread.authorize(creds) 
    spreadsheet = gc.open(GOOGLE_SHEET_NAME)
    worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
    logger.info(f"Pomyślnie połączono z Arkuszem Google: {GOOGLE_SHEET_NAME}")

    # --- 3c. Konfiguracja Google Drive ---
    # Budujemy usługę Drive przy użyciu tych samych danych logowania
    drive_service = build('drive', 'v3', credentials=creds)
    logger.info("Pomyślnie połączono z Google Drive")

    # Krok 1: Znajdź główny folder "Lokale" na "Mój Dysk"
    logger.info(f"Szukanie folderu: '{G_DRIVE_MAIN_FOLDER_NAME}'...")
    
    # Szukamy folderu na 'Mój Dysk' (bo teraz działamy jako Ty)
    response_folder = drive_service.files().list(
        q=f"name='{G_DRIVE_MAIN_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and 'root' in parents and trashed=False",
        spaces='drive',
        fields='files(id, name)',
    ).execute()
    
    files = response_folder.get('files', [])
    if not files:
        logger.critical(f"BŁĄD KRYTYCZNY: Nie znaleziono folderu '{G_DRIVE_MAIN_FOLDER_NAME}' na Twoim 'Mój Dysk'!")
        logger.critical(f"Upewnij się, że utworzyłeś folder '{G_DRIVE_MAIN_FOLDER_NAME}' na głównym poziomie 'Mój Dysk'.")
        exit()
    
    g_drive_main_folder_id = files[0].get('id')
    logger.info(f"Pomyślnie znaleziono folder '{G_DRIVE_MAIN_FOLDER_NAME}' (ID: {g_drive_main_folder_id})")

except Exception as e:
    logger.critical(f"BŁĄD KRYTYCZNY: Nie można połączyć z Google: {e}")
    logger.critical("Sprawdź, czy plik 'credentials.json' istnieje i czy API są włączone.")
    exit()


# --- 4. Konfiguracja Gemini (AI) ---
# (Bez zmian)
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

# --- 5. Definicja Promptu dla AI ---
# (Bez zmian)
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
3.  podmiot_odpowiedzialny: (np. "administracja", "serwis", "konserwator", "deweloper", "domhomegroup")
4.  Jeśli jakiejś informacji brakuje, wstaw w jej miejsce "BRAK DANYCH".
5.  Jeśli wiadomość to 'Rozpoczęcie odbioru', potraktuj to jako 'rodzaj_usterki' jeśli nie ma innej usterki.
6.  Nigdy nie dodawaj żadnego tekstu przed ani po obiekcie JSON. Ani '```json' ani '```'.

Wiadomość użytkownika do analizy znajduje się poniżej.
"""

# --- 6. Funkcja do Zapisu w Arkuszu ---
# (Bez zmian)
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

# --- FUNKCJA WYSYŁANIA NA GOOGLE DRIVE ---
# (Usunięto 'supportsAllDrives' - niepotrzebne, gdy działamy jako właściciel)
def upload_photo_to_drive(file_bytes, lokal_name, usterka_name, podmiot_name):
    """Wyszukuje podfolder lokalu i wysyła do niego zdjęcie."""
    global drive_service, g_drive_main_folder_id
    
    try:
        # Krok 1: Znajdź podfolder dla lokalu (np. "46.2")
        q_str = f"name='{lokal_name}' and mimeType='application/vnd.google-apps.folder' and '{g_drive_main_folder_id}' in parents and trashed=False"
        
        response = drive_service.files().list(
            q=q_str, 
            spaces='drive', 
            fields='files(id, name)',
        ).execute()
        
        lokal_folder = response.get('files', [])

        if not lokal_folder:
            logger.error(f"Nie znaleziono folderu dla lokalu: {lokal_name} wewnątrz '{G_DRIVE_MAIN_FOLDER_NAME}'")
            logger.error(f"Upewnij się, że utworzyłeś podfoldery (np. '46.2') wewnątrz folderu 'Lokale' na 'Mój Dysk'.")
            return False, f"Nie znaleziono folderu Drive dla '{lokal_name}'"

        lokal_folder_id = lokal_folder[0].get('id')
        
        # Krok 2: Przygotuj metadane i plik
        file_name = f"{usterka_name} - {podmiot_name}.jpg"
        file_metadata = {
            'name': file_name,
            'parents': [lokal_folder_id] 
        }
        
        # Krok 3: Wyślij plik
        file_bytes.seek(0)
        media = MediaIoBaseUpload(file_bytes, mimetype='image/jpeg', resumable=True)
        
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id',
        ).execute()
        
        logger.info(f"Pomyślnie wysłano plik '{file_name}' do folderu '{lokal_name}' (ID: {file.get('id')})")
        return True, file_name
    
    except Exception as e:
        logger.error(f"Błąd podczas wysyłania na Google Drive: {e}")
        return False, str(e)


# --- 7. Główny Handler (serce bota) ---
# (Bez zmian)
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
                 await update.message.reply_text("❌ Nie udało się rozpoznać lokalu lub firmy.\nSpróbuj ponownie, np: 'Rozpoczęcie odbioru, lokal 46/2, firma domhomegroup'.")
            else:
                # Normalizujemy nazwę lokalu, np. "Lokal 46/2" -> "46.2"
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
            
            await update.message.reply_text(f"➕ Dodano (tekst): '{usterka_opis}'\n(Łącznie: {len(chat_data['odbiur_usterki'])}). Wpisz kolejną lub 'Koniec odbioru'.")
            return 

    except json.JSONDecodeError as json_err:
        logger.error(f"Błąd parsowania JSON od Gemini (w logice sesji): {json_err}. Odpowiedź AI: {response.text}")
        await update.message.reply_text("❌ Błąd analizy AI. Spróbuj sformułować wiadomość inaczej.")
        return
    except Exception as session_err:
        logger.error(f"Wystąpił nieoczekiwany błąd w logice sesji: {session_err}")
        await update.message.reply_text(f"❌ Wystąpił krytyczny błąd: {session_err}")
        return

    # --- LOGIKA DOMYŚLNA (FALLBACK) ---
    # (Bez zmian)
    
    logger.info(f"Brak aktywnego odbioru. Przetwarzanie jako pojedyncze zgłoszenie: '{user_message}'")
    
    try:
        await update.message.reply_text("Przetwarzam jako pojedyncze zgłoszenie... 🧠")
        
        logger.info("Wysyłanie do Gemini...")
        response = model.generate_content([PROMPT_SYSTEMOWY, user_message])
        
        cleaned_text = response.text.strip().replace("```json", "").replace("```", "").strip()
        dane = json.loads(cleaned_text)
        logger.info(f"Gemini zwróciło JSON: {dane}")

        if zapisz_w_arkuszu(dane, message_time):
            await update.message.reply_text(f"✅ Zgłoszenie (pojedyncze) przyjęte i zapisane:\n\n"
                                          f"Lokal: {dane.get('numer_lokalu_budynku')}\n"
                                          f"Usterka: {dane.get('rodzaj_usterki')}\n"
                                          f"Podmiot: {dane.get('podmiot_odpowiedzialny')}")
        else:
            await update.message.reply_text("❌ Błąd zapisu do bazy danych (Arkusza). Skontaktuj się z adminem.")

    except json.JSONDecodeError:
        logger.error(f"Błąd parsowania JSON od Gemini (fallback). Odpowiedź AI: {response.text}")
        await update.message.reply_text("❌ Błąd analizy AI (fallback). Spróbuj sformułować zgłoszenie inaczej.")
    except Exception as e:
        logger.error(f"Wystąpił nieoczekiwany błąd (fallback): {e}")
        await update.message.reply_text(f"❌ Wystąpił krytyczny błąd (fallback): {e}")


# --- 7b. NOWY HANDLER DLA ZDJĘĆ ---
# (Bez zmian)
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Przechwytuje zdjęcie W TRAKCIE aktywnej sesji odbioru."""
    chat_data = context.chat_data
    
    if not chat_data.get('odbiur_aktywny'):
        await update.message.reply_text("Wyślij zdjęcie *po* rozpoczęciu odbioru. Teraz ta fotka zostanie zignorowana.")
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
        
        success, message = upload_photo_to_drive(file_bytes_io, lokal, usterka, podmiot)
        
        if success:
            chat_data['odbiur_usterki'].append(f"{usterka} (zdjęcie)")
            
            await update.message.reply_text(f"✅ Zdjęcie zapisane na Drive jako: '{message}'\n"
                                          f"➕ Usterka dodana do listy: '{usterka} (zdjęcie)'\n"
                                          f"(Łącznie: {len(chat_data['odbiur_usterki'])}).")
        else:
            await update.message.reply_text(f"❌ Błąd Google Drive: {message}")
            
    except Exception as e:
        logger.error(f"Błąd podczas przetwarzania zdjęcia: {e}")
        await update.message.reply_text(f"❌ Wystąpił błąd przy pobieraniu zdjęcia: {e}")


# --- 8. Uruchomienie Bota ---
# (Bez zmian)
def main():
    """Główna funkcja uruchamiająca bota."""
    
    logger.info("Uruchamianie bota...")
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("Bot nasłuchuje...")
    application.run_polling()

if __name__ == '__main__':
    main()