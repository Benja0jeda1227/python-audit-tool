# Multilingual Business Research Automation — Starter

Proyecto de demostración para portafolio de Upwork. Descubre **páginas candidatas** de fabricantes de lácteos en Francia, EE. UU. y Chile. Las búsquedas se hacen en el idioma local. No considera empresas verificadas los resultados de la API.

## Requisitos

- Windows con Python 3.10+ y VS Code (o cualquier editor)
- Clave API de Tavily (para búsquedas reales). El modo demo no necesita clave.

## Instalación con PowerShell (desde esta carpeta)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Si PowerShell no permite activar el entorno virtual, se pueden ejecutar los comandos usando `\.venv\Scripts\python.exe` sin activarlo:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe research.py --demo
```

## Primera prueba sin API, sin Internet y sin gasto

```powershell
python research.py --demo
```

Abre `output/candidates_DEMO.csv`. Los registros son **ficticios**, están claramente marcados y **no** deben publicarse.

## Búsqueda con datos reales

1. Crea una cuenta en https://app.tavily.com/ y copia tu API key.
2. Duplica `.env.example` y renombra la copia `.env`.
3. Sustituye `pega_aqui_tu_clave` por la clave API (sin comillas). No la subas a GitHub ni la compartas en capturas.
4. Ejecuta:

```powershell
python research.py
```

El programa ejecuta **3 consultas** (una por país) con `search_depth=basic`, recoge hasta 8 resultados por consulta y guarda `output/candidates.csv` y `output/summary.txt`. Los resultados podrían ser directorios, artículos o enlaces irrelevantes. **No significa que hayan sido verificados**.

Si necesitas más candidatos, antes de editar el CSV original y sin gastar créditos a ciegas:

```powershell
python research.py --queries-per-country 2
```

ATENCIÓN: este segundo comando sobrescribe `output/candidates.csv`. Si ya lo estás editando, guarda una copia con otro nombre antes de volver a ejecutar.

## De candidato a empresa verificada

1. Importa `output/candidates.csv` a Google Sheets (Archivo > Importar > Subir).
2. Lee cada resultado y determina si es una empresa fabricante, no solo un artículo o directorio.
3. Abre la web oficial, registra el nombre de empresa, los productos, URL oficial y URL de evidencia.
4. Si no hay información suficiente, conserva `Needs review` o escribe `Unverified`.
5. Si puedes documentar los campos esenciales, cambia `Verification Status` a `Verified` y coloca fecha en `Reviewed On`.
6. Quita candidatos que sean directorios/duplicados o no cumplan el criterio.
7. Crea una pestaña Resumen con cantidad por país, número verificado y enlaces pendientes.
8. Busca **10 empresas verificadas por país**, pero prioriza exactitud sobre cuota. No declares 30 si no las conseguiste.

### Criterio mínimo para Verified
- Nombre oficial y país comprobados.
- Evidencia de que fabrica productos alimenticios.
- Sitio oficial confirmado.
- URL específica que respalde el dato y fecha de consulta.

## Qué puedes presentar en Upwork

- Captura de planilla con fuentes y estados de verificación (una vez revisada).
- Resumen de países y empresas realmente verificadas.
- Enlace al código en GitHub, **sin** `.env` y sin datos privados.
- Descripción honesta: "Independent project using a search API to discover candidates and manual review to verify official company information."

## Límites y conducta responsable

- Tavily facilita descubrimiento, no garantiza exhaustividad ni veracidad.
- Este starter no hace scraping, no visita automáticamente sitios de las empresas y no extrae correos.
- Respeta términos de la API, condiciones de las fuentes, privacidad y licencias antes de reutilizar o comercializar datos.
- No recolectes información personal innecesaria, no inventes campos y no afirmes que una empresa fue verificada sin revisión.
- El código no tiene costo, pero las APIs pueden tener cuotas gratuitas y consumos variables.
