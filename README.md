# Exportador de perfil de Instagram

Aplicación de escritorio pequeña para recopilar los datos de un perfil que la sesión actual puede consultar, guardar un informe JSON y, si el usuario lo solicita, enviar la biografía y las descripciones seleccionadas a Gemini para una lectura lingüística exploratoria.

El programa guarda únicamente el **número** de seguidores y seguidos; no descarga las listas de cuentas. El análisis Big Five no es una prueba psicométrica, un diagnóstico ni una descripción fiable de la personalidad real de alguien.

## Requisitos

- Python 3.10 o posterior.
- Windows, macOS o Linux. La interfaz usa Tkinter, que normalmente viene incluido con Python; en algunas distribuciones de Linux se instala como paquete aparte.
- Una sesión propia de Instagram si las consultas de perfil requieren autenticación.
- Una clave de Gemini solo si se desea activar ese análisis.

## Instalación en Windows

Abre PowerShell en la carpeta del proyecto y ejecuta:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Si PowerShell bloquea la activación del entorno, puedes omitir la activación y usar directamente `\.venv\Scripts\python.exe` en los comandos siguientes.

Edita `.env` con un editor de texto. El archivo debe estar junto a `main.py` y debe contener valores propios:

```dotenv
INSTAGRAM_COOKIES=
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.5-flash-lite
```

`INSTAGRAM_COOKIES` debe ser la cadena de cookies de una sesión tuya, obtenida desde el navegador en el que ya iniciaste sesión. No compartas esa cadena: permite actuar como esa sesión hasta que expire o la revoques. `GEMINI_API_KEY` es opcional. `.env` está excluido de Git; confirma con `git status --ignored` si quieres revisar archivos ignorados.

## Iniciar la aplicación

Interfaz gráfica:

```powershell
python gui.py
```

En la ventana escribe el @handle, el límite de publicaciones/reels y pulsa **Iniciar extracción**. La casilla de Gemini es opcional; si la activas, la ventana pide confirmación antes de enviar el texto.

También se puede ejecutar el flujo de consola:

```powershell
python main.py
```

Reintentar Gemini a partir de un informe JSON existente, sin volver a consultar Instagram:

```powershell
python main.py --reanalyze sausage_player
```

Si el SDK de Gemini no está instalado en el intérprete que ejecuta el proyecto, instala las dependencias usando ese mismo intérprete:

```powershell
python -m pip install -r requirements.txt
```

## Datos y archivos generados

El JSON se guarda como `output/instagram_report_<usuario>.json`. Incluye los metadatos disponibles del perfil, los conteos de seguidores y seguidos, el contenido recopilado, hashtags, menciones, fecha, URL y métricas cuando Instagram las entrega. Las fotos y videos seleccionados se guardan en `output/media/<usuario>/posts/` o `output/media/<usuario>/reels/`.

El informe diferencia la cantidad solicitada de la cantidad recopilada. Un estado `limit_reached` indica que se alcanzó el límite configurado; no significa que se hayan descargado todas las publicaciones de la cuenta. Un conteo ausente o una métrica `null` significa que la respuesta no proporcionó ese dato.

## Cómo funciona

- **Interfaz:** Tkinter, incluido en la biblioteca estándar de Python.
- **Solicitudes web:** `curl_cffi`, un cliente HTTP con una interfaz parecida a Requests. Se usa para las consultas web y para transferir medios; este repositorio no automatiza un navegador.
- **Análisis opcional:** SDK oficial `google-genai` para llamar a Gemini y solicitar una respuesta JSON.
- **Salida:** JSON local y archivos multimedia seleccionados.

Este proyecto no usa Scrapy, Selenium ni Playwright. Esas herramientas tienen otros enfoques: Scrapy organiza rastreadores y pipelines de extracción; Selenium y Playwright automatizan navegadores. La implementación actual usa solicitudes HTTP directas. Una cookie mantiene el estado de sesión en solicitudes autorizadas, pero no concede permiso para ver contenido restringido ni elimina los controles de Instagram.

## Privacidad, límites y uso responsable

- Usa la herramienta solo con perfiles y datos que tengas autorización para analizar y conforme a las reglas aplicables.
- No intenta eludir controles de acceso. Los errores `401`/`login_required` indican que la sesión no tiene acceso a esa consulta; un `403`/`429` puede indicar una restricción temporal. Si aparece un `429`, detén las consultas y espera antes de probar de nuevo.
- Solo recopila conteos de seguidores/seguidos, no las listas de handles. No descarga comentarios.
- La biografía y las descripciones se envían a Gemini únicamente si se solicita el análisis y se confirma el envío. El informe local guarda la respuesta del análisis, pero no la clave API.
- El texto público no basta para inferir de forma fiable rasgos psicológicos. Las puntuaciones, si se generan, son aproximaciones especulativas de lenguaje y no deben usarse para decisiones de empleo, salud, educación, crédito o acceso a servicios.
- No publiques informes que contengan nombres, descripciones, enlaces u otros datos personales sin autorización.

## Dependencias

Las dependencias de Python están en [`requirements.txt`](requirements.txt). Para registrar cambios, revisa `git status` y evita añadir `.env`, cookies, claves API, `output/` o medios descargados.

## Informe del proyecto

El informe explicativo de control de calidad se distribuye por separado. Los archivos dentro de `output/` se excluyen de Git porque pueden contener datos personales o medios descargados. La evidencia revisada corresponde a una corrida de 10 elementos y muestra que Gemini falló porque faltaba `google-genai`; no documenta una extracción de 25 elementos ni un análisis completado.
