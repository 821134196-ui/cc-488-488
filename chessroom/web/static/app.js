/* 清点格点击切换 在场/缺件；提交时汇总为 present[件名][格位]。 */
(function () {
  var form = document.getElementById('slotForm');
  if (form) {
    form.addEventListener('click', function (e) {
      var cell = e.target.closest('.slot');
      if (!cell || cell.hasAttribute('data-ro')) return;
      var present = cell.classList.toggle('present');
      cell.classList.remove('missing', 'closed-missing');
      if (!present) cell.classList.add('missing');
      cell.textContent = present ? '✓' : '';
    });
    form.addEventListener('submit', function () {
      var data = {};
      form.querySelectorAll('.slot').forEach(function (cell) {
        var name = cell.dataset.name, slot = cell.dataset.slot;
        (data[name] = data[name] || {})[slot] = cell.classList.contains('present') ? 1 : 0;
      });
      var input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'present_json';
      input.value = JSON.stringify(data);
      form.appendChild(input);
    });
  }

  /* 损坏照片：选图后立即转 base64 放入隐藏字段（表单为 urlencoded）。 */
  var photo = form && form.querySelector('input[type=file][name=d_photo]');
  if (photo) {
    photo.addEventListener('change', function () {
      var old = form.querySelector('input[name=photo_b64]');
      if (old) old.remove();
      var file = photo.files[0];
      if (!file) return;
      var reader = new FileReader();
      reader.onload = function () {
        var hidden = document.createElement('input');
        hidden.type = 'hidden';
        hidden.name = 'photo_b64';
        hidden.value = String(reader.result).split(',')[1] || '';
        form.appendChild(hidden);
      };
      reader.readAsDataURL(file);
    });
  }

  /* 计时端：只显示剩余时间，分钟以下变红提示。 */
  var dial = document.getElementById('dial');
  if (dial) {
    var end = parseInt(dial.dataset.end, 10) * 1000;
    var rem = document.getElementById('rem');
    function tick() {
      var s = Math.max(0, Math.round((end - Date.now()) / 1000));
      var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
      rem.textContent = (h > 0 ? h + ':' + String(m).padStart(2, '0') : m) + ' 分';
      dial.classList.toggle('warn', s <= 10 * 60);
    }
    tick();
    setInterval(tick, 1000);
  }
})();
