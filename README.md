# Guardia LLM Tester

Runner para ejecutar automáticamente los 62 casos de evaluación contra el mismo endpoint que usa
el comprobador de GuardIA/Karasena.

El modo predeterminado no utiliza Chrome, Playwright, ChatGPT ni la extensión. Reproduce una
petición cURL capturada desde una sesión manual válida y sustituye exclusivamente el campo `text`.
La respuesta de Karasena se interpreta directamente:

- `{"valido": false}` → `block`.
- `{"valido": true}` → `allow`.

## Estructura

```text
evalsets/*.md                     Evalsets independientes, uno por caso de uso
.auth/karasena.fetch.js           cURL privado con la sesión (ignorado por Git)
guardia_tester/api.py             Cliente del endpoint /api/comprobador
guardia_tester/parser.py          Parser y selección de casos
guardia_tester/runner.py          CLI y ejecución
guardia_tester/report.py          Informes HTML, CSV y JSON
tests/                            Pruebas unitarias locales
results/                          Informes (ignorados por Git)
```

## Requisitos

- Python 3.11 o posterior.
- Una sesión manual válida en <https://guardia.karasena.com/guardia/verificaciones>.

El modo API usa únicamente la biblioteca estándar de Python; no necesita instalar paquetes.

## Capturar la petición

1. Entra manualmente en Karasena desde tu Chrome habitual.
2. Abre las herramientas de desarrollo con `F12` y selecciona **Network/Red**.
3. Ejecuta una comprobación de texto cualquiera.
4. Localiza la petición `POST` a `/api/comprobador`.
5. Botón derecho → **Copy** → **Copy as cURL (bash)**.
6. Reemplaza el contenido de `.auth/karasena.fetch.js` por el cURL completo y guarda.
7. Ejecuta inmediatamente las pruebas:

```powershell
python run_tests.py
```

Karasena emite actualmente tokens con una duración aproximada de cinco minutos. El runner valida
la caducidad antes de empezar. Si indica que el token ha caducado, repite la captura; no es
necesario modificar código ni volver a configurar Google.

Si el token caduca durante una batería, el runner se pausa antes de registrar el caso como error:

1. Captura otro **Copy as cURL (bash)** desde la sesión abierta de Karasena.
2. Reemplaza `.auth/karasena.fetch.js` y guarda.
3. Pulsa `ENTER` en el runner.

El caso que quedó pendiente se reintenta y las pasadas anteriores se conservan. Si escribes `S`
para terminar, la pasada parcial se descarta por completo: no aparece como `INCOMPLETE` ni altera
las estadísticas. Para ejecuciones no interactivas puede utilizarse:

```powershell
python run_tests.py --token-expiry stop
```

El archivo puede contener cookies y tokens. No lo publiques, no lo adjuntes al chat y no lo
incluyas en Git. La carpeta `.auth/` ya está excluida mediante `.gitignore`.

## Validar los casos sin llamar a Karasena

```powershell
python run_tests.py --dry-run
```

La salida esperada es:

```text
Casos cargados: 62 (37 block, 25 allow)
Markdown válido. No se ha abierto el navegador.
```

## Seleccionar un evalset

Cada caso de uso puede tener su propio archivo Markdown dentro de `evalsets/`. Para ver los
disponibles:

```powershell
python run_tests.py --list-evalsets
```

Ejecutar uno por identificador:

```powershell
python run_tests.py --evalset datos-medicos
python run_tests.py --evalset finanzas
```

El identificador se obtiene del nombre del archivo. Por ejemplo,
`GuardIA_EvalSet_Datos_Medicos.md` se convierte en `datos-medicos`. Si hay varios evalsets y no se
indica ninguno, el runner muestra un menú interactivo. También se admite una ruta explícita:

```powershell
python run_tests.py --cases evalsets/GuardIA_EvalSet_Finanzas.md
```

Los resultados se separan automáticamente:

```text
results/datos-medicos/<fecha-hora>/
results/finanzas/<fecha-hora>/
```

## Ejecutar pruebas

Batería completa. De forma predeterminada realiza **5 pasadas** de los 62 casos (310 muestras):

```powershell
python run_tests.py
```

Cambiar el número de repeticiones:

```powershell
python run_tests.py --repetitions 1
python run_tests.py --repetitions 5
python run_tests.py --repetitions 10
```

Cinco repeticiones es el valor recomendado para la regresión habitual con el token actual. Diez
o más proporcionan una estimación mejor, pero normalmente requieren una autenticación de pruebas
con mayor duración. Una pasada completa tarda actualmente unos 53 segundos.

Casos o grupos concretos:

```powershell
python run_tests.py --case A01
python run_tests.py --group I --group J
python run_tests.py --group M --case K01
```

Detenerse en el primer fallo:

```powershell
python run_tests.py --fail-fast
```

Para ver todos los parámetros:

```powershell
python run_tests.py --help
```

## Resultados

El proceso devuelve código `0` si todo coincide, `1` si hay fallos funcionales o técnicos y `2`
si existe un problema de configuración o autenticación.

En `results/<evalset>/<fecha-hora>/` se generan:

- `report.html`: resumen visual y detalle por caso.
- `summary.csv`: una fila por prompt con tasas de acierto, `block` y `allow`.
- `results.csv`: todos los intentos individuales, compatibles con Excel.
- `results.json`: resultados y métricas para integraciones.

La última tabla del HTML muestra una fila por prompt y una columna por repetición (`R1`, `R2`,
etc.) para visualizar la evolución `block/allow`. Los informes históricos pueden regenerarse sin
llamar de nuevo a Karasena:

```powershell
python -m guardia_tester.regenerate results
```

Las métricas incluyen accuracy global y por grupo, falsos positivos, falsos negativos, recall de
bloqueo y tasa de falsos positivos. Cada prompt se clasifica además como:

- `STABLE_PASS`: todas las repeticiones fueron correctas.
- `FLAKY`: el resultado alternó entre correcto e incorrecto.
- `STABLE_FAIL`: todas las repeticiones fueron incorrectas.
- `INCOMPLETE`: alguna repetición tuvo un error técnico.

## Formato de los casos

El Markdown sigue siendo la fuente única:

````markdown
### A01 — Título
INPUT:
"""
Texto que se enviará al comprobador.
"""
EXPECTED: block · subtype: lab · signals: [patient_identifier] · nota: explicación.
````

Se admiten `block` y `allow`. El parser falla si falta `INPUT`, `EXPECTED` o si hay identificadores
duplicados.

## Pruebas del runner

```powershell
python -m unittest discover -s tests -v
```

Estas pruebas no llaman a Karasena ni leen el archivo privado de autenticación.

## Adaptador de navegador legado

El código conserva `--adapter browser` como alternativa, pero no es necesario para el flujo
principal. Requiere instalar Playwright por separado y no resuelve el login automatizado de Google.
