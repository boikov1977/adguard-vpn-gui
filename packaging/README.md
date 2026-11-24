# Сборка .deb

Пакет собирается обычным `dpkg-deb`, без debhelper и без изменения системы.

## Требования к сборочной машине

- `dpkg-deb` (пакет `dpkg`), `rsync`;
- `/usr/bin/python3` (используется только для чтения версии из `agvpn/util.py`).

```bash
sudo apt install dpkg-dev rsync
```

## Сборка

Из корня репозитория:

```bash
packaging/build-deb.sh
```

Результат: `dist/adguard-vpn-gui_<version>_all.deb`, где `<version>` берётся из
`APP_VERSION` в `agvpn/util.py` — в скрипте версия не захардкожена.

Другой каталог для результата:

```bash
packaging/build-deb.sh --output /tmp/deb-out
```

## Проверка без установки

```bash
dpkg-deb --info dist/adguard-vpn-gui_*.deb
dpkg-deb --contents dist/adguard-vpn-gui_*.deb

TMP="$(mktemp -d)"
dpkg-deb --extract dist/adguard-vpn-gui_*.deb "$TMP"
/usr/bin/python3 -c "import sys; sys.path.insert(0, '$TMP/usr/lib/adguard-vpn-gui'); import agvpn.app"

desktop-file-validate packaging/adguard-vpn-gui.desktop
appstreamcli validate --no-net packaging/com.adguardvpn.gui.metainfo.xml
```

## Установка и удаление

```bash
sudo apt install ./dist/adguard-vpn-gui_1.2.3_all.deb
# или
sudo dpkg -i dist/adguard-vpn-gui_1.2.3_all.deb

sudo apt remove adguard-vpn-gui
sudo apt purge adguard-vpn-gui
```

Пользовательские настройки (`~/.config/adguard-vpn-gui`) и данные официального
CLI (`~/.local/share/adguardvpn-cli`) при удалении пакета не затрагиваются.

Запуск после установки: меню приложений → **AdGuard VPN** или
`/usr/bin/adguard-vpn-gui`. Скрипт `install.sh` для установки в `~/.local`
продолжает работать независимо от пакета.

## Состав пакета

| Путь | Назначение |
|---|---|
| `/usr/lib/adguard-vpn-gui/adguard-vpn-gui.py` | точка входа из исходников |
| `/usr/lib/adguard-vpn-gui/agvpn/` | пакет приложения, `assets/`, `style.css` |
| `/usr/bin/adguard-vpn-gui` | исполняемый Python-лаунчер (не симлинк) |
| `/usr/share/applications/adguard-vpn-gui.desktop` | пункт меню |
| `/usr/share/icons/hicolor/{scalable,256x256,128x128,64x64,48x48,32x32}/apps/adguard-vpn-gui.*` | иконки |
| `/usr/share/metainfo/com.adguardvpn.gui.metainfo.xml` | метаданные AppStream |
| `/usr/share/doc/adguard-vpn-gui/{copyright,LICENSE}` | лицензия и копирайт |
| `/usr/share/doc/adguard-vpn-gui/changelog.Debian.gz` | история изменений пакета |

Лаунчер специально сделан обычным исполняемым файлом: автозапуск в приложении
формирует команду из `sys.executable` и `sys.argv[0]`, то есть
`/usr/bin/python3 /usr/bin/adguard-vpn-gui --hidden`. Симлинк на
`adguard-vpn-gui.py` тоже работал бы, но тогда `sys.argv[0]` указывал бы в
`/usr/lib`, а не на стабильный путь `/usr/bin`.

## Зависимости

`Depends`: `python3`, `python3-gi`, `python3-cairo`, `python3-gi-cairo`,
`gir1.2-gtk-3.0`, `gir1.2-ayatanaappindicator3-0.1`, `hicolor-icon-theme`
(пакет ставит иконки именно в `hicolor`, без него они не находятся в меню и
трее).

`Recommends`: `adwaita-icon-theme` (symbolic-иконки интерфейса),
`gir1.2-notify-0.7` (всплывающие уведомления), `xdg-utils` (запасной вариант
открытия ссылок и файлов, если нет портала).

Официальный `adguardvpn-cli` указан в `Suggests`, а не в `Depends`/`Recommends`:
готового пакета в репозиториях Debian/Ubuntu обычно нет, он ставится установщиком
AdGuard. Жёсткая зависимость сделала бы `.deb` неустанавливаемым через `apt`,
поэтому CLI остаётся необязательной рекомендацией, а приложение показывает
подсказку, если его нет.

## Иконки и скриншоты

Иконки берутся из `agvpn/assets/` (`icon.svg`, `icon-256.png`, `icon-128.png`,
`icon-64.png`, `icon-48.png`, `icon-32.png`). Скриншоты для AppStream лежат в
`docs/screenshots/` (`dashboard.png`, `locations.png`) и публикуются по ссылкам
`raw.githubusercontent.com` из `com.adguardvpn.gui.metainfo.xml`.

Скриншоты снимаются с реального окна 1172×723 после изменения интерфейса.
Перед публикацией строка с адресом аккаунта в боковой панели размывается.

Ссылки на скриншоты в метаданных и `Homepage` указывают на публичный
`github.com/boikov1977/adguard-vpn-gui`. Если репозиторий приватный, эти URL
будут отдавать 404, и AppStream-каталоги не смогут скачать изображения. Для
публикации нужен публичный репозиторий (или замена ссылок на любой публично
доступный хостинг). На локальную установку и запуск приложения это не влияет.

## CI

GitHub Actions собирает пакет на каждом push и pull request
(`.github/workflows/ci.yml`): после тестов и `compileall` выполняются
`packaging/build-deb.sh`, `dpkg-deb -I`, `dpkg-deb -c`, а также
`desktop-file-validate` и `appstreamcli validate --no-net` (если инструменты
доступны; в workflow они устанавливаются через apt). Пакет в CI не
устанавливается и никуда не публикуется. Версия берётся из `APP_VERSION`,
поэтому после её поднятия до 1.2.3 CI автоматически соберёт
`adguard-vpn-gui_1.2.3_all.deb`.

## Права файлов

Скрипт выставляет каталоги `0755`, обычные файлы `0644`, а исполняемыми оставляет
только `/usr/bin/adguard-vpn-gui` и `/usr/lib/adguard-vpn-gui/agvpn/askpass.py`
(его напрямую запускает `sudo` через `SUDO_ASKPASS`). В конце сборки эти режимы
проверяются, и сборка падает, если они нарушены.

`postinst` один раз выполняет `python3 -m compileall -q /usr/lib/adguard-vpn-gui`,
чтобы байткод лежал рядом с исходниками и Python не компилировал их при каждом
запуске.
