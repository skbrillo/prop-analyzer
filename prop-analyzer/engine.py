import csv, json, math, statistics, pathlib, datetime
ROOT=pathlib.Path(__file__).parent
DATA=ROOT/'data'; SITE=ROOT/'docs'
def normal_cdf(x): return (1+math.erf(x/math.sqrt(2)))/2
def american_profit(odds): return 100/abs(odds) if odds<0 else odds/100
def implied(odds): return 1/(1+american_profit(odds))
def run():
    logs=list(csv.DictReader(open(DATA/'game_logs.csv',newline='')))
    odds=list(csv.DictReader(open(DATA/'sample_odds.csv',newline='')))
    output=[]
    for bet in odds:
        games=sorted((g for g in logs if g['player']==bet['player'] and g['date']<bet['game_date']),key=lambda g:g['date'])
        if len(games)<10: continue
        market=bet['market']; vals=[float(g[market]) for g in games]
        recent=vals[-10:]; last5=vals[-5:]
        projection=.5*statistics.mean(recent)+.3*statistics.mean(vals)+.2*statistics.mean(last5)
        sigma=max(2.0,statistics.stdev(vals[-20:]) if len(vals[-20:])>1 else 2.0)
        line=float(bet['line']); over=max(.01,min(.99,1-normal_cdf((line+.5-projection)/sigma)))
        under=1-over
        for side,p in [('Over',over),('Under',under)]:
            odds_value=int(bet['over_odds'] if side=='Over' else bet['under_odds'])
            profit=american_profit(odds_value); ev=p*profit-(1-p)
            hits=sum((v>line if side=='Over' else v<line) for v in recent)
            output.append({'player':bet['player'],'team':bet['team'],'opponent':bet['opponent'],'date':bet['game_date'],'market':market.title(),'side':side,'line':line,'odds':odds_value,'projection':round(projection,2),'probability':round(p,4),'implied_probability':round(implied(odds_value),4),'ev':round(ev,4),'last10':hits,'sample_size':len(vals),'data_source':'SYNTHETIC DEMO','model':'baseline-normal-v1'})
    output.sort(key=lambda x:x['ev'],reverse=True)
    SITE.mkdir(exist_ok=True)
    (SITE/'results.json').write_text(json.dumps({'generated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'mode':'DEMO — synthetic player logs and sample odds; not betting recommendations','results':output},indent=2))
    print(f'Generated {len(output)} demonstration outcomes')
if __name__=='__main__': run()
