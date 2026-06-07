from flask import Flask, render_template, request, jsonify
from game_engine import game, StockDataFetcher
from strategies import STRATEGIES

app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/add_stock', methods=['POST'])
def add_stock():
    try:
        data = request.get_json()
        code = data.get('code', '')
        ok, msg = game.add_stock(code)
        return jsonify({'success': ok, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'message': f'添加失败: {str(e)}'})

@app.route('/api/start_game', methods=['POST'])
def start_game():
    data = request.get_json()
    sd = data.get('start_date', '')
    ic = float(data.get('init_cash', 100000))
    lb = int(data.get('chart_lookback', 100))
    ok, msg = game.start_game(sd, ic, lb)
    return jsonify({'success': ok, 'message': msg})

@app.route('/api/state', methods=['GET'])
def get_state():
    return jsonify(game.get_full_state())

@app.route('/api/buy', methods=['POST'])
def buy():
    d = request.get_json()
    ok, msg = game.buy_stock(d.get('code',''), float(d.get('amount',0)))
    return jsonify({'success': ok, 'message': msg})

@app.route('/api/sell', methods=['POST'])
def sell():
    d = request.get_json()
    ok, msg = game.sell_stock(d.get('code',''), float(d.get('amount',0)))
    return jsonify({'success': ok, 'message': msg})

@app.route('/api/advance', methods=['POST'])
def advance():
    d = request.get_json()
    strat = d.get('auto_strategy', '')
    if strat and strat != 'off':
        game.auto_pilot = True
        game.auto_strategy = strat
    ok, msg = game.advance_day(int(d.get('days',1)))
    return jsonify({'success': ok, 'message': msg})

@app.route('/api/auto_pilot', methods=['POST'])
def auto_pilot():
    d = request.get_json()
    strat = d.get('strategy', 'off')
    if strat == 'off':
        game.auto_pilot = False
    else:
        game.auto_pilot = True
        game.auto_strategy = strat
        # Reset strategy state (clear cooldown, highest, etc)
        s = STRATEGIES.get(strat)
        if s: s.__init__()
    return jsonify({'success': True})

@app.route('/api/skip', methods=['POST'])
def skip():
    # Check auto-pilot
    game.advance_day(1)
    return jsonify({'success': True})

@app.route('/api/reset', methods=['POST'])
def reset():
    game.reset()
    return jsonify({'success': True})

@app.route('/api/remove_stock', methods=['POST'])
def remove_stock():
    d = request.get_json()
    code = d.get('code', '')
    ok, msg = game.remove_stock(code)
    return jsonify({'success': ok, 'message': msg})

@app.route('/api/chart_data', methods=['POST'])
def chart_data():
    d = request.get_json()
    result = game.get_historical_prices(d.get('code',''), int(d.get('lookback',200)))
    return jsonify({'data': result})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5123, debug=True)
