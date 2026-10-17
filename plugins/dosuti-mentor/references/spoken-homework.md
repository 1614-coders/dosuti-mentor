# Устная домашка

Если отдельного условия нет, прочитай интервал с заданием, соседние реплики и
проверь, кто говорит. Не превращай случайный совет, вопрос ученика или пример
в обязательное ДЗ. Если непонятны объём, сроки или критерии — оставь вопрос
открытым, не добавляй их от себя. При сомнениях не принимать draft как задание.

```sh
python -B plugins/dosuti-mentor/scripts/course.py spoken-homework draft --course COURSE --lesson LESSON --time 6270 --window 90
python -B plugins/dosuti-mentor/scripts/course.py spoken-homework accept --course COURSE --lesson LESSON --assignment-file TASK.json --expected-revision 0
python -B plugins/dosuti-mentor/scripts/course.py spoken-homework status --course COURSE --lesson LESSON
```

Сохрани draft вне папки лекции. Заполни instructions только подтверждаемым заданием,
review.status=reviewed, review.notes фактической сверкой и uncertainties сомнениями.
source содержит исходный ASR с ID/таймкодами, SHA видео и revision; не менять его.
accept повторно сверяет источник, ожидаемую редакцию и сохраняет UTF-8 условие.
Это проверенная человеком/ассистентом трактовка ASR, не дословно проверенная речь.
Если слушал звук, укажи это в notes; само наличие ASR этого не доказывает.

Файл преподавателя из attach имеет приоритет. Иначе context/status возвращают
принятую устную задачу как homework.status=provided с source_kind=lecture_speech,
origin, uncertainties, assurance и revision verbal:<hash>. Обычная проверка
задания, прогресс, зависимости и новые unit-ссылки привязываются к этой редакции.
Новая редакция (даже с тем же текстом) требует повторной проверки старого зачёта.
Поломанный источник или текст дают needs_review, а не разрешение выставить оценку.

Реестр COURSE/verbal-homework/ и его text/ находятся вне sealed lesson inventory:
задание можно добавить после удаления медиа, используя сохранённый ASR. Старый
compact.json и lesson.json не переписываются. Переноси курс целиком вместе с этим
реестром. Новая запись не создаёт оценок и не изменяет учебную цель/прогресс.
Предыдущие тексты сохраняются; автоматического удаления истории нет.
